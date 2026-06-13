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

            for current_code, group_df in grouped:
                code_idx += 1
                group_df = group_df.reset_index(drop=True)
                n = len(group_df)
                
                # 매 10종목마다 프로그레스 업데이트
                if progress_callback and code_idx % max(1, total_codes // 20) == 0:
                    prog = 30 + int((code_idx / total_codes) * 60)
                    progress_callback(prog, f"시뮬레이션 진행 중... ({code_idx}/{total_codes})")

                for i in range(10, n):
                    # 현재 틱 정보
                    current_row = group_df.iloc[i]
                    current_price = current_row['tic_close']
                    current_time_str = current_row['datetime']
                    
                    # 1. 매수 평가 (포지션이 없을 때)
                    if current_code not in portfolio:
                        # 300 틱 윈도우 슬라이싱
                        start_idx = max(0, i - 300)
                        history_df = group_df.iloc[start_idx:i+1].copy()
                        
                        locals_dict = prepare_buy_strategy_locals(current_code, history_df, realtime_metrics=None)
                        
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
                        history_df = group_df.iloc[start_idx:i+1].copy()
                        
                        buy_record = {
                            'buy_price': pos['buy_price'],
                            'qty': pos['qty'],
                            'amount': pos['buy_price'] * pos['qty'],
                            'datetime': pos['buy_time'],
                            'strategy': pos['stg']
                        }
                        
                        locals_dict = prepare_sell_strategy_locals(
                            current_code, history_df, buy_record, real_profit_pct, current_price, realtime_metrics=None
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
