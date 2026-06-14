import sqlite3
import pandas as pd
import numpy as np
import time
from datetime import datetime
import json
import logging
from config_manager import EnvConfigParser

logger = logging.getLogger("Backtester")
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s: [백테스터] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
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
        
        # SQLite에 중복 컬럼이 저장되었을 경우(예: 과거 버그로 인한 중복) 방어
        df = df.loc[:, ~df.columns.duplicated()]
        
        return df

    def run(self, start_date, end_date, code='ALL', progress_callback=None, custom_buy=None, custom_sell=None):
        try:
            logger.info(f"백테스트 데이터 로딩 시작: {start_date} ~ {end_date} (종목: {code})")
            if progress_callback: progress_callback(10, "데이터를 로딩 중입니다...")
                
            df = self.load_data(start_date, end_date, code)
            if df.empty:
                if progress_callback: progress_callback(100, "해당 기간에 데이터가 없습니다.")
                return {"error": "해당 기간에 데이터가 없습니다."}
                
            if progress_callback: progress_callback(30, f"데이터 로딩 완료: {len(df):,} 틱. 시뮬레이션 준비 중...")

            buy_strategies = []
            sell_strategies = []
            
            # 1. 커스텀 전략이 명시적으로 제공된 경우 (웹 대시보드에서 동적 주입)
            if custom_buy is not None and custom_sell is not None:
                buy_strategies = custom_buy
                sell_strategies = custom_sell
                logger.info("웹 대시보드에서 전달된 커스텀 전략을 적용합니다.")
            else:
                # 2. 제공되지 않은 경우 settings.json의 기본값 로드
                from strategy_utils import load_strategies_from_config
                stg_name = self.config.get('SETTINGS', 'LAST_STRATEGY', fallback='기본_돌파')
                
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
            eval_errors = 0
            debug_logs = []

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
                first_eval_logged = False
                # 1. 컬럼명 일괄 변경
                group_df = group_df.rename(columns={
                    'tic_open': 'open', 'tic_high': 'high', 'tic_low': 'low', 
                    'tic_close': 'close', 'tic_volume': 'volume'
                })
                
                # 1.5 3분봉 데이터 처리
                # DB에 실시간 누적 스냅샷(min3_volume 등)이 이미 있으면 그대로 사용
                # 없으면 resample로 재집계 (과거 데이터 호환)
                try:
                    group_df['datetime'] = pd.to_datetime(group_df['datetime'])
                    
                    # DB에 min3_ 기본 OHLCV 컬럼이 존재하는지 확인
                    has_db_min3 = all(
                        col in group_df.columns 
                        for col in ['min3_open', 'min3_close', 'min3_volume']
                    )
                    
                    if has_db_min3 and group_df['min3_volume'].notna().sum() > 0:
                        # DB의 실시간 누적 스냅샷을 그대로 사용 (실시간 매매와 동일한 환경)
                        logger.debug(f"📊 {current_code}: DB의 실시간 min3_ 스냅샷 데이터 사용")
                    else:
                        # DB에 min3_ 데이터가 없는 경우: resample로 재집계 (과거 데이터 호환)
                        logger.debug(f"📊 {current_code}: min3_ 데이터 없음, resample로 재집계")
                        temp_df = group_df.set_index('datetime')
                        
                        # 3분봉 집계 (ohlcv)
                        min3_df = temp_df.resample('3min').agg({
                            'open': 'first',
                            'high': 'max',
                            'low': 'min',
                            'close': 'last',
                            'volume': 'sum'
                        }).ffill()
                        
                        # 분봉 지표 계산
                        min3_inds = KiwoomIndicatorExtractor.extract_chart_indicators(min3_df)
                        for k, v in min3_inds.items():
                            min3_df[k] = v
                            
                        # 컬럼명에 min3_ 접두사 추가 및 소문자 변환
                        min3_df.columns = [f'min3_{c.lower()}' for c in min3_df.columns]
                        
                        # 기존 min3_ 컬럼 제거 후 재집계 데이터로 교체
                        existing_min3 = [c for c in temp_df.columns if c.startswith('min3_')]
                        temp_df = temp_df.drop(columns=existing_min3)
                        
                        # 병합 (backward 매핑)
                        group_df = pd.merge_asof(temp_df, min3_df, left_index=True, right_index=True, direction='backward')
                        group_df = group_df.reset_index()
                except Exception as e:
                    logger.error(f"3분봉 데이터 처리 오류 ({current_code}): {e}")
                    
                # 2. 기술적 지표 1회 일괄(Batch) 계산
                try:
                    indicators = KiwoomIndicatorExtractor.extract_chart_indicators(group_df)
                    for k, v in indicators.items():
                        group_df[k] = v
                except Exception as e:
                    logger.error(f"지표 사전 계산 오류 ({current_code}): {e}")
                
                n = len(group_df)
                group_df['AI_SCORE'] = 0.0
                
                # 3. AI_SCORE 배치 계산 (전략에 AI_SCORE가 포함된 경우만)
                if uses_ai and LGBM_MODEL:
                    try:
                        f_strength = np.zeros(n)  # tic_strength 삭제됨 (AI 모델 호환용 0.0 주입)
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
                        
                        group_df['AI_SCORE'] = LGBM_MODEL.predict(mat, num_threads=1)
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
                
                # 5. 전략 문자열 사전 컴파일 (루프 내 속도 최적화)
                buy_compiled = []
                for stg in buy_strategies:
                    try:
                        buy_compiled.append((stg['name'], compile(stg['content'], '<string>', 'eval')))
                    except Exception as e:
                        logger.error(f"매수 전략 컴파일 오류 ({stg['name']}): {e}")
                
                sell_compiled = []
                for stg in sell_strategies:
                    try:
                        sell_compiled.append((stg['name'], float(stg.get('partial_sell_ratio', 1.0)), compile(stg['content'], '<string>', 'eval')))
                    except Exception as e:
                        logger.error(f"매도 전략 컴파일 오류 ({stg['name']}): {e}")
                
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
                        
                        for stg_name_str, stg_code in buy_compiled:
                            try:
                                if eval(stg_code, globals(), locals_dict):
                                    buy_signal = True
                                    matched_stg_name = stg_name_str
                                    break
                            except Exception as e:
                                eval_errors += 1
                                if eval_errors <= 5:
                                    err_msg = f"매수 평가 오류 ({stg_name_str}): {e}"
                                    logger.error(err_msg)
                                    debug_logs.append(f"[{current_time_str}] {current_code} {err_msg}")
                                
                        if not first_eval_logged:
                            debug_logs.append(f"[{current_code}] 첫 평가 샘플 - 시간: {current_time_str}, 가격: {current_price}, AI_SCORE: {locals_dict.get('AI_SCORE', 0.0)}")
                            first_eval_logged = True

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
                    elif current_code in portfolio:
                        pos = portfolio[current_code]
                        buy_price = pos['buy_price']
                        profit_pct = (current_price - buy_price) / buy_price * 100.0
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
                        locals_dict['current_profit_pct'] = real_profit_pct
                        locals_dict['buy_price'] = pos['buy_price']
                        locals_dict['buy_time'] = pos['buy_time']
                        locals_dict['holding_amount'] = pos['buy_price'] * pos['qty']

                        sell_signal = False
                        sell_ratio = 1.0
                        matched_sell_stg = ""
                        for stg_name_str, stg_ratio, stg_code in sell_compiled:
                            try:
                                if eval(stg_code, globals(), locals_dict):
                                    sell_signal = True
                                    sell_ratio = stg_ratio
                                    matched_sell_stg = stg_name_str
                                    break
                            except Exception as e:
                                eval_errors += 1
                                if eval_errors <= 5:
                                    err_msg = f"매도 평가 오류 ({stg_name_str}): {e}"
                                    logger.error(err_msg)
                                    debug_logs.append(f"[{current_time_str}] {current_code} {err_msg}")
                                
                        # 장 마감 직전 (15:18:00 이후) 강제 청산 (포맷: YYYY-MM-DD HH:MM:SS)
                        time_part = current_time_str[11:16].replace(":", "") if len(current_time_str) >= 16 else ""
                        if time_part >= "1518":
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
                                    'qty': sell_qty,
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
                                    
                # 해당 종목의 틱 루프(for i in range(10, n))가 모두 종료된 후 잔여 포지션이 있다면 강제 청산
                if current_code in portfolio:
                    pos = portfolio[current_code]
                    sell_qty = pos['qty']
                    trade_profit = (current_price - pos['buy_price']) * sell_qty
                    trade_profit -= (pos['buy_price'] * sell_qty * 0.002)
                    total_profit += trade_profit
                    capital += trade_profit
                    
                    if trade_profit > 0: win_count += 1
                    else: loss_count += 1
                    
                    real_profit_pct = (current_price - pos['buy_price']) / pos['buy_price'] * 100.0 - 0.2
                    
                    trades.append({
                        'code': current_code,
                        'buy_time': pos['buy_time'],
                        'sell_time': current_time_str,
                        'buy_price': pos['buy_price'],
                        'sell_price': current_price,
                        'qty': sell_qty,
                        'profit_pct': real_profit_pct,
                        'profit_amount': trade_profit,
                        'buy_stg': pos['stg'],
                        'sell_stg': "백테스트 종료 강제청산"
                    })
                    
                    max_capital = max(max_capital, capital)
                    current_dd = (max_capital - capital) / max_capital * 100.0
                    mdd = max(mdd, current_dd)
                    del portfolio[current_code]

            # 모든 종목 루프 종료 후 결과 요약
            total_trades = win_count + loss_count
            win_rate = (win_count / total_trades * 100.0) if total_trades > 0 else 0.0
            
            # 매도 시간(sell_time) 기준으로 거래 내역 정렬
            trades = sorted(trades, key=lambda x: x['sell_time'])
            
            # 시계열 자산(Equity Curve) 생성 (초기 자본: 10,000,000 기준)
            equity_history = []
            current_equity = 10000000
            
            events = []
            for t in trades:
                events.append({'time': t['buy_time'], 'type': 'buy'})
                events.append({'time': t['sell_time'], 'type': 'sell', 'profit': t['profit_amount']})
                
            events = sorted(events, key=lambda x: x['time'])
            
            for e in events:
                if e['type'] == 'sell':
                    current_equity += e['profit']
                equity_history.append({
                    'time': e['time'],
                    'equity': current_equity
                })
            
            # --- Buy & Hold 벤치마크 (동일 비중 포트폴리오) 자산 시계열 생성 ---
            bnh_history = []
            try:
                df_bnh = df.copy()
                df_bnh['datetime'] = pd.to_datetime(df_bnh['datetime'])
                df_bnh['min3_time'] = df_bnh['datetime'].dt.floor('3min')
                if 'min3_close' in df_bnh.columns:
                    close_col = 'min3_close'
                    vol_col = 'min3_volume' if 'min3_volume' in df_bnh.columns else 'volume'
                elif 'tic_close' in df_bnh.columns:
                    close_col = 'tic_close'
                    vol_col = 'tic_volume' if 'tic_volume' in df_bnh.columns else 'volume'
                else:
                    close_col = 'close'
                    vol_col = 'volume'
                
                # 가중 평균 지수 계산을 위한 데이터 스냅샷
                min3_snap = df_bnh.groupby(['min3_time', 'code']).last().reset_index()
                
                # 가중치 설정 (거래대금 = 주가 * 거래량)
                # 거래량이 없거나 누락된 경우 최소 가중치(1) 부여
                min3_snap['weight'] = min3_snap[close_col] * min3_snap[vol_col].fillna(0)
                min3_snap['weight'] = min3_snap['weight'].replace(0, 1)
                
                def weighted_avg(g):
                    total_w = g['weight'].sum()
                    if total_w == 0:
                        return g[close_col].mean()
                    return (g[close_col] * g['weight']).sum() / total_w
                
                # 시점별 가중 평균 지수 산출
                min3_bnh = min3_snap.groupby('min3_time').apply(weighted_avg).reset_index(name='weighted_close')
                
                for _, row in min3_bnh.iterrows():
                    bnh_history.append({
                        'time': row['min3_time'].strftime('%Y-%m-%d %H:%M:%S'),
                        'equity': row['weighted_close'] # 프론트 파싱 호환을 위해 키는 equity 사용
                    })
            except Exception as e:
                logger.error(f"Buy&Hold 벤치마크 생성 오류: {e}")
            
            result = {
                "total_trades": total_trades,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate": round(win_rate, 2),
                "total_profit": round(total_profit, 0),
                "final_capital": round(capital, 0),
                "mdd": round(mdd, 2),
                "trades": trades, # 전체 내역 프론트로 전달
                "history": equity_history, # 자산 곡선 데이터
                "bnh_history": bnh_history, # Buy&Hold 벤치마크

                "uses_ai": uses_ai,
                "lgbm_model_loaded": LGBM_MODEL is not None,
                "debug_logs": debug_logs[-200:] # 프론트엔드 과부하 방지
            }
            
            if progress_callback: progress_callback(100, "시뮬레이션 완료!")
            return result
            
        except Exception as e:
            logger.error(f"백테스팅 오류: {e}", exc_info=True)
            return {"error": str(e), "debug_logs": [f"치명적 오류: {e}"]}

if __name__ == '__main__':
    bt = Backtester()
    def p(prog, msg): print(f"[{prog}%] {msg}")
    res = bt.run('2026-06-01', '2026-06-13', progress_callback=p)
    print("\n[백테스트 결과]")
    for k, v in res.items():
        if k != 'trades':
            print(f"{k}: {v}")
