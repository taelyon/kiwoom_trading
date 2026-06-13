import sqlite3
import pandas as pd
import numpy as np
import time
from datetime import datetime
import json
import logging
from config_manager import EnvConfigParser

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
            from strategy_utils import load_strategies_from_config
            
            # config.get은 (section, option) 두 개의 인자를 받습니다.
            stg_name = self.config.get('SETTINGS', 'LAST_STRATEGY', '기본_돌파')
            
            buy_strategies = []
            sell_strategies = []
            
            all_strategies = load_strategies_from_config()
            
            if stg_name in all_strategies:
                buy_strategies = all_strategies[stg_name]['buy_strategies']
                sell_strategies = all_strategies[stg_name]['sell_strategies']
            else:
                logger.warning(f"설정된 전략 '{stg_name}'을 찾을 수 없어 빈 전략으로 실행합니다.")
            
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
            
            # AI_SCORE 사용 여부 판단
            uses_ai = False
            for stg in buy_strategies + sell_strategies:
                if 'AI_SCORE' in stg.get('content', ''):
                    uses_ai = True
                    break
            
            for current_code, group_df in grouped:
                code_idx += 1
                group_df = group_df.reset_index(drop=True)
                
                # 1. 컬럼명 일괄 변경
                group_df = group_df.rename(columns={
                    'tic_open': 'open', 'tic_high': 'high', 'tic_low': 'low', 
                    'tic_close': 'close', 'tic_strength': 'strength'
                })
                
                # 2. 기술적 지표 1회 일괄(Batch) 계산
                try:
                    indicators = KiwoomIndicatorExtractor.extract_chart_indicators(group_df)
                    for k, v in indicators.items():
                        group_df[k] = v
                except Exception as e:
                    logger.error(f"지표 사전 계산 오류 ({current_code}): {e}")
                
                n = len(group_df)
                
                # 3. AI_SCORE 배치 계산 (전략에 AI_SCORE가 포함된 경우만)
                if uses_ai and LGBM_MODEL:
                    try:
                        f_strength = group_df['strength'].values if 'strength' in group_df.columns else np.zeros(n)
                        f_velocity = group_df['TICK_VELOCITY'].values if 'TICK_VELOCITY' in group_df.columns else np.full(n, 999999.0)
                        f_relative = np.zeros(n)
                        
                        vol = group_df['volume'].values if 'volume' in group_df.columns else np.ones(n)
                        f_spike = np.zeros(n)
                        if n > 10:
                            roll_avg = pd.Series(vol).rolling(window=10).mean().shift(1).fillna(1).values
                            roll_avg = np.where(roll_avg == 0, 1, roll_avg)
                            f_spike = vol / roll_avg
                        
                        f_vi_dist = group_df['VI_DISTANCE'].values if 'VI_DISTANCE' in group_df.columns else np.full(n, 999.0)
                        f_kosdaq_change = group_df['kosdaq_change'].values if 'kosdaq_change' in group_df.columns else np.zeros(n)
                        
                        close_vals = group_df['close'].values
                        vwap_vals = group_df['VWAP'].values if 'VWAP' in group_df.columns else close_vals.copy()
                        vwap_safe = np.where(vwap_vals == 0, 1e-9, vwap_vals)
                        f_vwap_dist = (close_vals - vwap_vals) / vwap_safe
                        
                        f_bb_pos = group_df['BB_POSITION'].values if 'BB_POSITION' in group_df.columns else np.full(n, 0.5)
                        f_macd_hist = group_df['MACD_HIST'].values if 'MACD_HIST' in group_df.columns else np.zeros(n)
                        f_rsi = group_df['RSI'].values if 'RSI' in group_df.columns else np.full(n, 50.0)
                        
                        dt_series = pd.to_datetime(group_df['datetime'], errors='coerce')
                        f_time = np.clip((dt_series.dt.hour * 60 + dt_series.dt.minute).values - 540, 0, 390)
                        
                        num_features = LGBM_MODEL.num_feature()
                        if num_features == 11:
                            mat = np.column_stack((f_strength, f_velocity, f_relative, f_spike, f_vi_dist, f_kosdaq_change, f_vwap_dist, f_bb_pos, f_macd_hist, f_rsi, f_time))
                        elif num_features == 10:
                            mat = np.column_stack((f_strength, f_velocity, f_relative, f_spike, f_vi_dist, f_kosdaq_change, f_vwap_dist, f_bb_pos, f_macd_hist, f_rsi))
                        elif num_features == 6:
                            mat = np.column_stack((f_strength, f_velocity, f_relative, f_spike, f_vi_dist, f_kosdaq_change))
                        else:
                            mat = np.zeros((n, num_features))
                        
                        group_df['AI_SCORE'] = LGBM_MODEL.predict(mat)
                    except Exception as e:
                        logger.error(f"AI_SCORE 배치 계산 오류 ({current_code}): {e}")
                        group_df['AI_SCORE'] = 0.0
                
                # 4. numpy 배열 사전 추출 (루프 내 iloc 호출 최소화)
                close_arr = group_df['close'].values
                datetime_arr = group_df['datetime'].values
                
                # 전략 eval에 사용할 모든 컬럼을 numpy 배열로 미리 추출
                precomputed = {}
                for col in group_df.columns:
                    try:
                        precomputed[col] = group_df[col].values
                    except Exception:
                        pass
                
                # 매 종목마다 프로그레스 업데이트
                if progress_callback:
                    prog = 30 + int((code_idx / total_codes) * 60)
                    progress_callback(prog, f"시뮬레이션 진행 중... ({code_idx}/{total_codes}) - {current_code}")
                
                for i in range(10, n):
                    current_price = float(close_arr[i])
                    current_time_str = str(datetime_arr[i])
                    
                    # 1. 매수 평가 (포지션이 없을 때)
                    if current_code not in portfolio:
                        # 경량 locals_dict 구성 (numpy 슬라이싱 = 메모리 복사 없음)
                        start_idx = max(0, i - 300)
                        locals_dict = {}
                        for col_name, col_arr in precomputed.items():
                            arr_slice = col_arr[start_idx:i+1]
                            locals_dict[col_name] = arr_slice
                            # tic_ 접두사 호환
                            if not col_name.startswith('tic_') and not col_name.startswith('min3_'):
                                locals_dict[f'tic_{col_name}'] = arr_slice
                        
                        # AI_SCORE는 스칼라로 제공
                        if 'AI_SCORE' in precomputed:
                            locals_dict['AI_SCORE'] = float(precomputed['AI_SCORE'][i])
                        
                        locals_dict['code'] = current_code
                        locals_dict['datetime'] = datetime.now()
                        locals_dict['current_price'] = current_price
                        
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
                        real_profit_pct = profit_pct - 0.2
                        
                        # 경량 locals_dict 구성
                        start_idx = max(0, i - 300)
                        locals_dict = {}
                        for col_name, col_arr in precomputed.items():
                            arr_slice = col_arr[start_idx:i+1]
                            locals_dict[col_name] = arr_slice
                            if not col_name.startswith('tic_') and not col_name.startswith('min3_'):
                                locals_dict[f'tic_{col_name}'] = arr_slice
                        
                        if 'AI_SCORE' in precomputed:
                            locals_dict['AI_SCORE'] = float(precomputed['AI_SCORE'][i])
                        
                        locals_dict['code'] = current_code
                        locals_dict['datetime'] = datetime.now()
                        locals_dict['current_price'] = current_price
                        locals_dict['profit_pct'] = real_profit_pct
                        locals_dict['buy_price'] = pos['buy_price']
                        locals_dict['buy_time'] = pos['buy_time']
                        locals_dict['holding_amount'] = pos['buy_price'] * pos['qty']

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
                                
                        # 장 마감 직전 (15:18:00 이후) 강제 청산
                        time_part = current_time_str[8:14] if len(current_time_str) >= 14 else ""
                        if time_part >= "151800":
                            sell_signal = True
                            sell_ratio = 1.0
                            matched_sell_stg = "장마감 강제청산"
                                
                        if sell_signal:
                            sell_qty = int(pos['qty'] * sell_ratio)
                            if sell_qty > 0:
                                trade_profit = (current_price - pos['buy_price']) * sell_qty
                                trade_profit -= (pos['buy_price'] * sell_qty * 0.002)
                                
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
