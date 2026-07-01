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

    def load_condition_history(self, start_date_str, end_date_str, code=None):
        try:
            conn = sqlite3.connect(self.db_path)
            
            start_datetime = f"{start_date_str} 00:00:00"
            end_datetime = f"{end_date_str} 23:59:59"
            
            query = f"""
            SELECT code, start_time as entry_time, end_time as exit_time
            FROM monitoring_history
            WHERE start_time <= '{end_datetime}'
              AND (end_time IS NULL OR end_time >= '{start_datetime}')
            """
            if code and code != 'ALL':
                query += f" AND code = '{code}'"
                
            df = pd.read_sql(query, conn)
            conn.close()
            return df
        except Exception as e:
            logger.error(f"감시 이력 로딩 실패: {e}")
            return pd.DataFrame()

    def run(self, start_date, end_date, code='ALL', progress_callback=None, custom_buy=None, custom_sell=None, initial_capital=10000000, buycount=3):
        try:
            logger.info(f"백테스트 데이터 로딩 시작: {start_date} ~ {end_date} (종목: {code})")
            if progress_callback: progress_callback(10, "데이터를 로딩 중입니다...")
                
            df = self.load_data(start_date, end_date, code)
            if df.empty:
                if progress_callback: progress_callback(100, "해당 기간에 데이터가 없습니다.")
                return {"error": "해당 기간에 데이터가 없습니다."}
                
            if progress_callback: progress_callback(30, f"데이터 로딩 완료: {len(df):,} 틱. 시뮬레이션 준비 중...")

            # 조건검색 이력 로드
            condition_history_df = self.load_condition_history(start_date, end_date, code)

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
            capital = initial_capital
            invested_per_trade = capital / max(1, buycount) # buycount 등분 분할 투자
            
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
            
            
            # --- 전략 문자열 사전 컴파일 ---
            buy_compiled = []
            for stg in buy_strategies:
                try:
                    buy_compiled.append((stg['name'], compile(stg['content'], '<string>', 'eval')))
                except Exception as e:
                    logger.error(f"매수 로직 컴파일 오류 ({stg['name']}): {e}")
            
            sell_compiled = []
            for stg in sell_strategies:
                try:
                    sell_compiled.append((stg['name'], float(stg.get('partial_sell_ratio', 1.0)), compile(stg['content'], '<string>', 'eval')))
                except Exception as e:
                    logger.error(f"매도 로직 컴파일 오류 ({stg['name']}): {e}")

            # --- Phase 1: Precomputation ---
            processed_dfs = []
            stock_data = {}
            
            for current_code, group_df in grouped:
                code_idx += 1
                group_df = group_df.reset_index(drop=True)
                first_eval_logged = False
                
                # 1. 컬럼명 일괄 변경
                group_df = group_df.rename(columns={
                    'tic_open': 'open', 'tic_high': 'high', 'tic_low': 'low', 
                    'tic_close': 'close', 'tic_volume': 'volume'
                })
                
                # 1.5 3분봉 데이터 존재 확인
                try:
                    group_df['datetime'] = pd.to_datetime(group_df['datetime'])
                    has_db_min3 = all(col in group_df.columns for col in ['min3_open', 'min3_close', 'min3_volume'])
                    if not has_db_min3 or group_df['min3_volume'].notna().sum() == 0:
                        logger.warning(f"{current_code}: DB에 3분봉 데이터가 불충분합니다. 지표 생성 없이 진행합니다.")
                except Exception as e:
                    logger.error(f"3분봉 데이터 확인 오류 ({current_code}): {e}")
                    
                # 감시 이력 기반 필터링 (IS_MONITORED)
                if condition_history_df.empty:
                    # 감시 이력 데이터가 아예 없는 경우 (과거 데이터 호환성 유지)
                    group_df['IS_MONITORED'] = True
                else:
                    group_df['IS_MONITORED'] = False
                    histories = condition_history_df[condition_history_df['code'] == current_code]
                    for _, hist_row in histories.iterrows():
                        entry_dt = pd.to_datetime(hist_row['entry_time'])
                        exit_dt = pd.to_datetime(hist_row['exit_time']) if pd.notna(hist_row['exit_time']) else pd.Timestamp.max
                        group_df['IS_MONITORED'] = group_df['IS_MONITORED'] | ((group_df['datetime'] >= entry_dt) & (group_df['datetime'] <= exit_dt))
                        
                # 2. DB 지표를 엔진 변수명으로 매핑 (재계산 방지)
                try:
                    # 틱 지표 및 분봉 지표 매핑 (tic_ma5 -> MA5, min3_rsi -> MIN3_RSI 등)
                    for col in group_df.columns:
                        if col.startswith('tic_'):
                            upper_name = col[4:].upper()
                            group_df[upper_name] = group_df[col]
                        elif col.startswith('min3_'):
                            upper_name = col.upper()
                            group_df[upper_name] = group_df[col]
                            
                    # 특수 지표 변수명 명시적 매핑
                    if 'tic_velocity' in group_df.columns:
                        group_df['TICK_VELOCITY'] = group_df['tic_velocity']
                    if 'tic_relative_position' in group_df.columns:
                        group_df['RELATIVE_POSITION'] = group_df['tic_relative_position']
                except Exception as e:
                    logger.error(f"지표 매핑 오류 ({current_code}): {e}")
                
                n = len(group_df)
                group_df['AI_SCORE'] = 0.0
                
                # 3. AI_SCORE 배치 계산
                if uses_ai and LGBM_MODEL:
                    try:
                        f_strength = group_df['tic_strength'].values if 'tic_strength' in group_df.columns else np.zeros(n)
                        f_velocity = group_df['TICK_VELOCITY'].values if 'TICK_VELOCITY' in group_df.columns else np.full(n, 999999.0)
                        f_relative = group_df['RELATIVE_POSITION'].values if 'RELATIVE_POSITION' in group_df.columns else np.zeros(n)
                        
                        vol = group_df['volume'].values if 'volume' in group_df.columns else np.ones(n)
                        f_spike = np.zeros(n)
                        if n > 10:
                            roll_avg = pd.Series(vol).rolling(window=10).mean().shift(1).fillna(1).values
                            roll_avg = np.where(roll_avg == 0, 1, roll_avg)
                            f_spike = vol / roll_avg
                        

                        close_vals = group_df['close'].values
                        vwap_vals = group_df['VWAP'].values if 'VWAP' in group_df.columns else close_vals.copy()
                        vwap_safe = np.where(vwap_vals == 0, 1e-9, vwap_vals)
                        f_vwap_dist = (close_vals - vwap_vals) / vwap_safe
                        
                        f_bb_pos = group_df['BB_POSITION'].values if 'BB_POSITION' in group_df.columns else np.full(n, 0.5)
                        f_macd_hist = group_df['MACD_HIST'].values if 'MACD_HIST' in group_df.columns else np.zeros(n)
                        f_rsi = group_df['RSI'].values if 'RSI' in group_df.columns else np.full(n, 50.0)
                        
                        dt_series = pd.to_datetime(group_df['datetime'], errors='coerce')
                        f_time = np.clip((dt_series.dt.hour * 60 + dt_series.dt.minute).values - 540, 0, 390)
                        
                        # [신규 추가] 파생 가속도 지표 (price_roc, vol_roc) 동기화
                        if 'close' in group_df.columns:
                            f_price_roc = group_df['close'].pct_change(periods=10).fillna(0.0).values
                        else:
                            f_price_roc = np.zeros(n)
                            
                        if 'volume' in group_df.columns:
                            vol_sum_5 = group_df['volume'].rolling(5).sum()
                            prev_vol_sum_5 = vol_sum_5.shift(5)
                            f_vol_roc = np.where(prev_vol_sum_5 > 0, vol_sum_5 / prev_vol_sum_5, 1.0)
                            f_vol_roc = pd.Series(f_vol_roc).fillna(1.0).values
                        else:
                            f_vol_roc = np.ones(n)
                        
                        num_features = LGBM_MODEL.num_feature()
                        
                        if num_features >= 17:
                            if 'MIN3_MA5' in group_df.columns and 'MIN3_MA20' in group_df.columns:
                                f_min3_trend_agree = ((group_df['MIN3_MA5'] > group_df['MIN3_MA20']) & (group_df['MIN3_MA20'] > 0)).astype(int).values
                            else:
                                f_min3_trend_agree = np.zeros(n)
                                
                            if 'MA5' in group_df.columns and 'MA20' in group_df.columns:
                                ma20_safe = np.where(group_df['MA20'] == 0, 1e-9, group_df['MA20'])
                                f_tic_ma_spread = ((group_df['MA5'] - group_df['MA20']) / ma20_safe).fillna(0.0).values
                            else:
                                f_tic_ma_spread = np.zeros(n)
                                
                            if 'close' in group_df.columns and 'volume' in group_df.columns:
                                amount = group_df['close'] * group_df['volume']
                                roll_amt = amount.rolling(10).mean().shift(1).fillna(1e-9).values
                                roll_amt = np.where(roll_amt == 0, 1e-9, roll_amt)
                                f_tic_amount_spike = (amount.values / roll_amt)
                                f_tic_amount_spike = np.nan_to_num(f_tic_amount_spike, nan=0.0, posinf=0.0, neginf=0.0)
                            else:
                                f_tic_amount_spike = np.zeros(n)
                                
                            if 'high' in group_df.columns and 'low' in group_df.columns and 'close' in group_df.columns:
                                hl_diff = group_df['high'] - group_df['low']
                                hl_safe = np.where(hl_diff == 0, 1e-9, hl_diff)
                                f_tic_tail_ratio = ((group_df['high'] - group_df['close']) / hl_safe).fillna(0.0).values
                            else:
                                f_tic_tail_ratio = np.zeros(n)
                                
                        if num_features == 15:
                            mat = np.column_stack((
                                f_strength, f_velocity, f_relative, f_spike,
                                f_vwap_dist, f_bb_pos, f_macd_hist, f_rsi, f_time,
                                f_price_roc, f_vol_roc,
                                f_min3_trend_agree, f_tic_ma_spread, f_tic_amount_spike, f_tic_tail_ratio
                            ))
                        elif num_features == 11:
                            mat = np.column_stack((
                                f_strength, f_velocity, f_relative, f_spike,
                                f_vwap_dist, f_bb_pos, f_macd_hist, f_rsi, f_time,
                                f_price_roc, f_vol_roc
                            ))
                        elif num_features == 9:
                            mat = np.column_stack((f_strength, f_velocity, f_relative, f_spike, f_vwap_dist, f_bb_pos, f_macd_hist, f_rsi, f_time))
                        elif num_features == 8:
                            mat = np.column_stack((f_strength, f_velocity, f_relative, f_spike, f_vwap_dist, f_bb_pos, f_macd_hist, f_rsi))
                        elif num_features == 4:
                            mat = np.column_stack((f_strength, f_velocity, f_relative, f_spike))
                        else:
                            logger.warning(f"⚠️ 백테스터에 {num_features}개 피처에 대한 행렬 매핑 로직이 구현되지 않았습니다. 기본 0.0 값으로 평가됩니다.")
                            mat = np.zeros((n, num_features))
                        
                        group_df['AI_SCORE'] = LGBM_MODEL.predict(mat, num_threads=1)
                    except Exception as e:
                        logger.error(f"AI_SCORE 배치 계산 오류 ({current_code}): {e}")
                
                # 4. precomputed 추출 (numpy 배열)
                precomputed = {}
                for col in group_df.columns:
                    try: precomputed[col] = group_df[col].values
                    except: pass
                
                stock_data[current_code] = {
                    'precomputed': precomputed,
                    'current_idx': 0,
                    'total_len': n,
                    'first_eval_logged': False
                }
                
                processed_dfs.append(group_df)
                
                if progress_callback:
                    prog = 10 + int((code_idx / total_codes) * 20)
                    progress_callback(prog, f"데이터 전처리 중... ({code_idx}/{total_codes}) - {current_code}")
            if not processed_dfs:
                return {"error": "처리할 수 있는 정상 데이터가 없습니다."}
                
            # 전체 통합 및 시간순 정렬
            full_df = pd.concat(processed_dfs, ignore_index=True)
            full_df = full_df.sort_values(by='datetime').reset_index(drop=True)
            
            # --- Phase 2: Event-Driven Simulation ---
            from config_manager import get_config
            time_settings = get_config().get_trading_time_settings()
            buy_end_time_str = time_settings['buy_end_time'].strftime('%H%M')
            sell_all_time_str = time_settings['sell_all_time'].strftime('%H%M')
            sell_all_enabled = time_settings['sell_all_enabled']
            
            grouped_by_time = full_df.groupby('datetime', sort=False)
            total_times = len(grouped_by_time)
            time_idx = 0
            
            current_date = None
            daily_blacklist = set()
            cooldown_list = {} # {code: expiration_time}
            
            for current_time, time_df in grouped_by_time:
                date_str = str(current_time)[:10]
                if current_date != date_str:
                    current_date = date_str
                    daily_blacklist.clear()
                    cooldown_list.clear()
                    
                time_idx += 1
                if progress_callback and time_idx % max(1, total_times // 20) == 0:
                    prog = 30 + int((time_idx / total_times) * 60)
                    progress_callback(prog, f"시뮬레이션 진행 중... ({time_idx}/{total_times}) 틱")
                
                current_time_str = str(current_time)
                time_part = current_time_str[11:16].replace(":", "") if len(current_time_str) >= 16 else ""
                is_market_close = (time_part >= "1518")
                is_lunch_time = ("1130" <= time_part < "1300") # 점심시간 체크
                is_buy_blocked_time = (time_part >= buy_end_time_str)
                is_force_sell_time = sell_all_enabled and (time_part >= sell_all_time_str)
                
                # 1. 매도 평가 (현재 보유 종목 중 time_df에 존재하는 것)
                for _, row in time_df.iterrows():
                    current_code = row['code']
                    if current_code in portfolio:
                        sd = stock_data[current_code]
                        idx = sd['current_idx']
                        current_price = float(row['close'])
                        
                        pos = portfolio[current_code]
                        buy_price = pos['buy_price']
                        
                        # highest_price 갱신
                        if current_price > pos.get('highest_price', buy_price):
                            pos['highest_price'] = current_price
                        
                        highest_price = pos['highest_price']
                        from_peak_pct = (current_price - highest_price) / highest_price * 100.0 if highest_price > 0 else 0.0
                        
                        real_profit_pct = (current_price - buy_price) / buy_price * 100.0 - 0.2
                        
                        # eval 호환성 locals_dict 구성
                        start_idx = max(0, idx - 300)
                        locals_dict = {}
                        for col_name, col_arr in sd['precomputed'].items():
                            arr_slice = col_arr[start_idx:idx+1]
                            locals_dict[col_name] = arr_slice
                            if not col_name.startswith('tic_') and not col_name.startswith('min3_'):
                                locals_dict[f'tic_{col_name}'] = arr_slice
                                
                        if 'AI_SCORE' in sd['precomputed']:
                            locals_dict['AI_SCORE'] = float(sd['precomputed']['AI_SCORE'][idx])
                            
                        locals_dict['code'] = current_code
                        locals_dict['datetime'] = datetime.now()
                        locals_dict['current_price'] = current_price
                        locals_dict['profit_pct'] = real_profit_pct
                        locals_dict['current_profit_pct'] = real_profit_pct
                        locals_dict['buy_price'] = buy_price
                        locals_dict['buy_time'] = pos['buy_time']
                        locals_dict['holding_amount'] = buy_price * pos['qty']
                        locals_dict['highest_price'] = highest_price
                        locals_dict['from_peak_pct'] = from_peak_pct
                        
                        sell_signal = False
                        sell_ratio = 1.0
                        matched_sell_stg = ""
                        
                        for stg_name_str, stg_ratio, stg_code in sell_compiled:
                            # 이미 발동된 이력이 있는 매도 룰은 평가에서 제외 (중복 방지)
                            if stg_name_str in pos.get('executed_sell_rules', set()):
                                continue
                                
                            try:
                                if eval(stg_code, globals(), locals_dict):
                                    sell_signal = True
                                    sell_ratio = stg_ratio
                                    matched_sell_stg = stg_name_str
                                    break
                            except Exception as e:
                                eval_errors += 1
                                if eval_errors <= 5:
                                    logger.error(f"매도 평가 오류 ({stg_name_str}): {e}")
                        
                        # 오버나잇 방지 강제 청산 및 장마감 강제 청산
                        if is_force_sell_time or is_market_close:
                            sell_signal = True
                            sell_ratio = 1.0
                            matched_sell_stg = "마감 강제청산"
                            
                        if sell_signal:
                            sell_qty = int(pos['qty'] * sell_ratio)
                            if sell_qty > 0:
                                trade_profit = (current_price - buy_price) * sell_qty
                                trade_profit -= (buy_price * sell_qty * 0.002)
                                total_profit += trade_profit
                                capital += trade_profit
                                if trade_profit > 0: win_count += 1
                                else: loss_count += 1
                                
                                trades.append({
                                    'code': current_code,
                                    'buy_time': pos['buy_time'],
                                    'sell_time': current_time_str,
                                    'buy_price': buy_price,
                                    'sell_price': current_price,
                                    'qty': sell_qty,
                                    'profit_pct': real_profit_pct,
                                    'profit_amount': trade_profit,
                                    'buy_stg': pos['stg'],
                                    'sell_stg': matched_sell_stg
                                })
                                max_capital = max(max_capital, capital)
                                mdd = max(mdd, (max_capital - capital) / max_capital * 100.0)
                                
                                if sell_ratio >= 0.99:
                                    del portfolio[current_code]
                                    if real_profit_pct < 0.0:
                                        daily_blacklist.add(current_code) # 당일 재매매 금지
                                    else:
                                        from datetime import timedelta
                                        cooldown_list[current_code] = current_time + timedelta(minutes=30) # 30분 쿨타임
                                else:
                                    portfolio[current_code]['qty'] -= sell_qty
                                    portfolio[current_code].setdefault('executed_sell_rules', set()).add(matched_sell_stg)
                                    
                # 2. 매수 평가 (보유 슬롯이 비어있고, 점심시간이 아닐 때만, 그리고 매수 차단 시간이 아닐 때만)
                if not is_market_close and not is_lunch_time and not is_buy_blocked_time:
                    if 'AI_SCORE' in time_df.columns:
                        buy_candidates = time_df.sort_values(by='AI_SCORE', ascending=False)
                    else:
                        buy_candidates = time_df
                        
                    for _, row in buy_candidates.iterrows():
                        if len(portfolio) >= buycount:
                            break # 보유 한도 도달
                            
                        current_code = row['code']
                        sd = stock_data[current_code]
                        idx = sd['current_idx']
                        
                        # 데이터가 10건 이상 쌓인 시점부터 매수 평가 (블랙리스트 및 쿨타임 제외)
                        if idx >= 10 and current_code not in portfolio and current_code not in daily_blacklist:
                            if current_code in cooldown_list and current_time < cooldown_list[current_code]:
                                continue # 쿨타임 중
                            # IS_MONITORED 체크 (조건검색 편입 기간 중일 때만 매수)
                            is_monitored = True
                            if 'IS_MONITORED' in sd['precomputed']:
                                is_monitored = sd['precomputed']['IS_MONITORED'][idx]
                                
                            if is_monitored:
                                current_price = float(row['close'])
                                start_idx = max(0, idx - 300)
                                locals_dict = {}
                                for col_name, col_arr in sd['precomputed'].items():
                                    arr_slice = col_arr[start_idx:idx+1]
                                    locals_dict[col_name] = arr_slice
                                    if not col_name.startswith('tic_') and not col_name.startswith('min3_'):
                                        locals_dict[f'tic_{col_name}'] = arr_slice
                                        
                                if 'AI_SCORE' in sd['precomputed']:
                                    locals_dict['AI_SCORE'] = float(sd['precomputed']['AI_SCORE'][idx])
                                
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
                                            logger.error(f"매수 평가 오류 ({stg_name_str}): {e}")
                                            
                                if not sd['first_eval_logged']:
                                    debug_logs.append(f"[{current_code}] 첫 평가 샘플 - 시간: {current_time_str}, 가격: {current_price}, AI_SCORE: {locals_dict.get('AI_SCORE', 0.0)}")
                                    sd['first_eval_logged'] = True
                                    
                                if buy_signal:
                                    qty = int(invested_per_trade / current_price)
                                    if qty > 0:
                                        portfolio[current_code] = {
                                            'buy_price': current_price,
                                            'qty': qty,
                                            'buy_time': current_time_str,
                                            'stg': matched_stg_name,
                                            'executed_sell_rules': set(),
                                            'highest_price': current_price
                                        }
                
                # 3. 현재 틱(time) 처리가 끝났으므로 해당 종목들의 내부 idx 1 증가
                for current_code in time_df['code'].unique():
                    stock_data[current_code]['current_idx'] += 1

            # 4. 잔여 포지션 청산 (장 종료 후 보유 종목)
            for current_code, pos in list(portfolio.items()):
                # 마지막 가격은 precomputed 의 마지막 값을 참조
                sd = stock_data[current_code]
                last_price = float(sd['precomputed']['close'][-1])
                last_time = str(sd['precomputed']['datetime'][-1])
                
                sell_qty = pos['qty']
                trade_profit = (last_price - pos['buy_price']) * sell_qty
                trade_profit -= (pos['buy_price'] * sell_qty * 0.002)
                total_profit += trade_profit
                capital += trade_profit
                if trade_profit > 0: win_count += 1
                else: loss_count += 1
                real_profit_pct = (last_price - pos['buy_price']) / pos['buy_price'] * 100.0 - 0.2
                
                trades.append({
                    'code': current_code,
                    'buy_time': pos['buy_time'],
                    'sell_time': last_time,
                    'buy_price': pos['buy_price'],
                    'sell_price': last_price,
                    'qty': sell_qty,
                    'profit_pct': real_profit_pct,
                    'profit_amount': trade_profit,
                    'buy_stg': pos['stg'],
                    'sell_stg': "백테스트 종료 강제청산"
                })
                max_capital = max(max_capital, capital)
                mdd = max(mdd, (max_capital - capital) / max_capital * 100.0)
            
# 모든 종목 루프 종료 후 결과 요약
            total_trades = win_count + loss_count
            win_rate = (win_count / total_trades * 100.0) if total_trades > 0 else 0.0
            
            # 매도 시간(sell_time) 기준으로 거래 내역 정렬
            trades = sorted(trades, key=lambda x: x['sell_time'])
            
            # 시계열 자산(Equity Curve) 생성
            equity_history = []
            current_equity = initial_capital
            
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
