import sqlite3
import pandas as pd
import numpy as np
import time
from datetime import datetime
import json
import logging
from config_manager import EnvConfigParser
from strategy_utils import prepare_buy_strategy_locals, prepare_sell_strategy_locals, LGBM_MODEL

def parse_strategies(stg_str):
    try:
        if not stg_str or stg_str.strip() == '': return []
        return json.loads(stg_str)
    except Exception:
        return []

logger = logging.getLogger("Backtester")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

class Backtester:
    def __init__(self):
        self.config = EnvConfigParser()
        self.db_path = 'data/stock_data.db'
        
    def load_data(self, start_date_str, end_date_str, code=None):
        conn = sqlite3.connect(self.db_path)
        
        # DB 저장 포맷 (YYYY-MM-DD HH:MM:SS) 처리를 위해 변환
        start_datetime = f"{start_date_str} 00:00:00"
        end_datetime = f"{end_date_str} 23:59:59"
        
        query = f"""
        SELECT *
        FROM stock_data 
        WHERE datetime >= '{start_datetime}' AND datetime <= '{end_datetime}'
        """
        if code and code != 'ALL':
            query += f" AND code = '{code}'"
            
        query += " ORDER BY datetime ASC"
            
        df = pd.read_sql(query, conn)
        conn.close()
        
        # 호환성을 위한 컬럼 변환 (DB -> DataFrame)
        # strategy_utils에서 volume 컬럼을 사용하므로 tic_volume을 volume으로 매핑
        if 'tic_volume' in df.columns and 'volume' not in df.columns:
            df['volume'] = df['tic_volume']
            
        return df

    def run(self, start_date, end_date, code='ALL', progress_callback=None):
        try:
            logger.info(f"백테스트 데이터 로딩 시작: {start_date} ~ {end_date} (종목: {code})")
            if progress_callback: progress_callback(10, "데이터를 로딩 중입니다...")
                
            df = self.load_data(start_date, end_date, code)
            if df.empty:
                if progress_callback: progress_callback(100, "해당 기간에 데이터가 없습니다.")
                return {"error": "해당 기간에 데이터가 없습니다."}
                
            if progress_callback: progress_callback(30, f"데이터 로딩 완료: {len(df):,} 틱. 시뮬레이션 준비 중...")

            # 전략 로드
            buy_strategies = parse_strategies(self.config.get('SETTINGS_BUY_STRATEGY', '[]'))
            sell_strategies = parse_strategies(self.config.get('SETTINGS_SELL_STRATEGY', '[]'))
            
            if not buy_strategies:
                stg_name = self.config.get('SETTINGS_LAST_STRATEGY', '기본_돌파')
                buy_strategies = parse_strategies(self.config.get(f'STRATEGY_{stg_name}_BUY_STG', '[]'))
                sell_strategies = parse_strategies(self.config.get(f'STRATEGY_{stg_name}_SELL_STG', '[]'))

            uses_ai = False
            for stg in buy_strategies + sell_strategies:
                if 'AI_SCORE' in stg.get('content', ''):
                    uses_ai = True
                    break
            if uses_ai:
                logger.info("⚠️ 백테스팅 전략에 AI_SCORE가 포함되어 있어 추론 시간이 길어질 수 있습니다.")

            portfolio = {} # code -> {'buy_price': float, 'qty': int, 'buy_time': str}
            trades = []
            
            # 종목별 시뮬레이션을 위해 분리
            grouped = df.groupby('code')
            total_codes = len(grouped)
            code_idx = 0
            
            total_profit = 0.0
            win_count = 0
            loss_count = 0
            capital = 10000000 # 초기 자본금 1000만원 가정
            invested_per_trade = 1000000 # 1회 진입 금액 100만원
            
            max_capital = capital
            mdd = 0.0

            from strategy_utils import KiwoomIndicatorExtractor, LGBM_MODEL
            
            for current_code, group_df in grouped:
                code_idx += 1
                group_df = group_df.reset_index(drop=True)
                
                # 1. 컬럼명 일괄 변경 (rename 오버헤드 제거)
                group_df = group_df.rename(columns={
                    'tic_open': 'open', 'tic_high': 'high', 'tic_low': 'low', 
                    'tic_close': 'close', 'tic_strength': 'strength'
                })
                
                # 2. 기술적 지표 1회 일괄(Batch) 계산 (루프 내 talib 재계산 방지)
                try:
                    indicators = KiwoomIndicatorExtractor.extract_chart_indicators(group_df)
                    for k, v in indicators.items():
                        group_df[k] = v
                except Exception as e:
                    logger.error(f"지표 사전 계산 오류 ({current_code}): {e}")
                    
                n = len(group_df)
                
                # 3. AI_SCORE Batch 연산 (추론 시간이 오래 걸리는 것 방지)
                if uses_ai and LGBM_MODEL:
                    try:
                        f_strength = group_df['strength'].values if 'strength' in group_df.columns else np.zeros(n)
                        f_velocity = group_df['TICK_VELOCITY'].values if 'TICK_VELOCITY' in group_df.columns else np.full(n, 999999.0)
                        f_imbalance = group_df['ORDER_BOOK_IMBALANCE'].values if 'ORDER_BOOK_IMBALANCE' in group_df.columns else np.zeros(n)
                        f_relative = np.zeros(n) # min3_relative_position 대체값
                        
                        vol = group_df['volume'].values
                        f_spike = np.zeros(n)
                        if n > 10:
                            prev_10_avg = group_df['volume'].rolling(window=10).mean().shift(1).fillna(1).values
                            prev_10_avg = np.where(prev_10_avg == 0, 1, prev_10_avg)
                            f_spike = vol / prev_10_avg
                            
                        f_vi_dist = group_df['VI_DISTANCE'].values if 'VI_DISTANCE' in group_df.columns else np.full(n, 999.0)
                        f_kosdaq_change = group_df['kosdaq_change'].values if 'kosdaq_change' in group_df.columns else np.zeros(n)
                        
                        close_vals = group_df['close'].values
                        vwap_vals = group_df['VWAP'].values if 'VWAP' in group_df.columns else close_vals
                        vwap_vals_safe = np.where(vwap_vals == 0, 1e-9, vwap_vals)
                        f_vwap_dist = (close_vals - vwap_vals) / vwap_vals_safe
                        
                        f_bb_pos = group_df['BB_POSITION'].values if 'BB_POSITION' in group_df.columns else np.zeros(n)
                        f_macd_hist = group_df['MACD_HIST'].values if 'MACD_HIST' in group_df.columns else np.zeros(n)
                        f_rsi = group_df['RSI'].values if 'RSI' in group_df.columns else np.full(n, 50.0)
                        
                        dt_series = pd.to_datetime(group_df['datetime'])
                        f_time = (dt_series.dt.hour * 60 + dt_series.dt.minute) - (9 * 60)
                        f_time = np.clip(f_time.values, 0, 390)
                        
                        num_features = LGBM_MODEL.num_feature()
                        if num_features == 11:
                            input_matrix = np.column_stack((f_strength, f_velocity, f_relative, f_spike, f_vi_dist, f_kosdaq_change, f_vwap_dist, f_bb_pos, f_macd_hist, f_rsi, f_time))
                        elif num_features == 10:
                            input_matrix = np.column_stack((f_strength, f_velocity, f_relative, f_spike, f_vi_dist, f_kosdaq_change, f_vwap_dist, f_bb_pos, f_macd_hist, f_rsi))
                        elif num_features == 6:
                            input_matrix = np.column_stack((f_strength, f_velocity, f_relative, f_spike, f_vi_dist, f_kosdaq_change))
                        elif num_features == 5:
                            input_matrix = np.column_stack((f_strength, f_velocity, f_imbalance, f_relative, f_spike))
                        else:
                            input_matrix = np.zeros((n, num_features))
                            
                        group_df['AI_SCORE'] = LGBM_MODEL.predict(input_matrix)
                    except Exception as e:
                        logger.error(f"AI_SCORE Batch 계산 오류 ({current_code}): {e}")
                        group_df['AI_SCORE'] = 0.0
                
                # 매 종목마다 프로그레스 업데이트 (멈춘 것처럼 보이지 않게)
                progress_msg = f"시뮬레이션 진행 중... ({code_idx}/{total_codes}) - {current_code}"
                if code_idx % max(1, total_codes // 10) == 0:
                    logger.info(progress_msg)
                
                if progress_callback:
                    prog = 30 + int((code_idx / total_codes) * 60)
                    progress_callback(prog, progress_msg)

                for i in range(10, n):
                    # 현재 틱 정보
                    current_row = group_df.iloc[i]
                    current_price = current_row['close'] # renamed from tic_close
                    current_time_str = current_row['datetime']
                    
                    # 1. 매수 평가 (포지션이 없을 때)
                    if current_code not in portfolio:
                        # 300 틱 윈도우 슬라이싱 (copy 생략으로 속도 극대화)
                        start_idx = max(0, i - 300)
                        api_df = group_df.iloc[start_idx:i+1]
                        
                        locals_dict = prepare_buy_strategy_locals(
                            current_code, api_df, pd.DataFrame(), 
                            realtime_metrics=None, skip_ai=not uses_ai
                        )
                        
                        # 강제 호환성
                        if 'datetime' not in locals_dict:
                            locals_dict['datetime'] = datetime.now()
                            
                        buy_signal = False
                        matched_stg_name = ""
                        
                        for stg in buy_strategies:
                            try:
                                if eval(stg['content'], globals(), locals_dict):
                                    buy_signal = True
                                    matched_stg_name = stg['name']
                                    break
                            except Exception:
                                pass
                                
                        if buy_signal:
                            # 매수 체결
                            qty = int(invested_per_trade / current_price)
                            if qty > 0:
                                portfolio[current_code] = {
                                    'buy_price': current_price,
                                    'qty': qty,
                                    'buy_time': current_time_str,
                                    'stg': matched_stg_name
                                }
                    
                    # 2. 매도 평가 (포지션이 있을 때)
                    else:
                        pos = portfolio[current_code]
                        profit_pct = (current_price - pos['buy_price']) / pos['buy_price'] * 100.0
                        # 수수료/세금 대략 0.2% 차감 적용
                        real_profit_pct = profit_pct - 0.2
                        
                        start_idx = max(0, i - 300)
                        api_df = group_df.iloc[start_idx:i+1]
                        
                        buy_record = {
                            'buy_price': pos['buy_price'],
                            'qty': pos['qty'],
                            'amount': pos['buy_price'] * pos['qty'],
                            'datetime': pos['buy_time'],
                            'strategy': pos['stg']
                        }
                        locals_dict = prepare_sell_strategy_locals(
                            current_code, api_df, pd.DataFrame(), buy_record, real_profit_pct, current_price, 
                            realtime_metrics=None, skip_ai=not uses_ai
                        )
                        
                        if 'datetime' not in locals_dict:
                            locals_dict['datetime'] = datetime.now()

                        sell_signal = False
                        sell_ratio = 1.0
                        matched_sell_stg = ""
                        
                        for stg in sell_strategies:
                            try:
                                if eval(stg['content'], globals(), locals_dict):
                                    sell_signal = True
                                    sell_ratio = stg.get('partial_sell_ratio', 1.0)
                                    matched_sell_stg = stg['name']
                                    break
                            except Exception:
                                pass
                                
                        # 장 마감 직전 (15:18:00 이후) 강제 청산 조건 추가
                        time_part = current_time_str[8:14] if len(current_time_str) >= 14 else ""
                        if time_part >= "151800":
                            sell_signal = True
                            sell_ratio = 1.0
                            matched_sell_stg = "장마감 강제청산"
                                
                        if sell_signal:
                            # 매도 체결
                            sell_qty = int(pos['qty'] * sell_ratio)
                            if sell_qty > 0:
                                trade_profit = (current_price - pos['buy_price']) * sell_qty
                                trade_profit -= (pos['buy_price'] * sell_qty * 0.002) # 수수료/세금
                                
                                total_profit += trade_profit
                                capital += trade_profit
                                
                                if trade_profit > 0: win_count += 1
                                else: loss_count += 1
                                
                                trades.append({
                                    'code': current_code,
                                    'buy_time': pos['buy_time'],
                                    'sell_time': current_time_str,
                                    'buy_price': pos['buy_price'],
                                    'sell_price': current_price,
                                    'profit_pct': real_profit_pct,
                                    'profit_amount': trade_profit,
                                    'buy_stg': pos['stg'],
                                    'sell_stg': matched_sell_stg
                                })
                                
                                max_capital = max(max_capital, capital)
                                current_dd = (max_capital - capital) / max_capital * 100.0
                                mdd = max(mdd, current_dd)
                                
                                if sell_ratio >= 0.99:
                                    del portfolio[current_code]
                                else:
                                    portfolio[current_code]['qty'] -= sell_qty

            # 결과 요약
            total_trades = win_count + loss_count
            win_rate = (win_count / total_trades * 100.0) if total_trades > 0 else 0.0
            
            result = {
                "total_trades": total_trades,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate": round(win_rate, 2),
                "total_profit": round(total_profit, 0),
                "final_capital": round(capital, 0),
                "mdd": round(mdd, 2),
                "trades": trades[-50:] # 최근 50개만 프론트로 전달 (용량 방지)
            }
            
            if progress_callback: progress_callback(100, "시뮬레이션 완료!")
            return result
            
        except Exception as e:
            logger.error(f"백테스팅 오류: {e}", exc_info=True)
            return {"error": str(e)}

if __name__ == '__main__':
    bt = Backtester()
    def p(prog, msg): print(f"[{prog}%] {msg}")
    res = bt.run('2026-06-01', '2026-06-13', progress_callback=p)
    print("\n[백테스트 결과]")
    for k, v in res.items():
        if k != 'trades':
            print(f"{k}: {v}")
