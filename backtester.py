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

def format_backtest_indicator_log(code, strategy_name, strategy_type, condition, safe_locals):
    """실전 매매 실시간 로그와 동일한 판단 근거 지표 출력 포맷터"""
    log_lines = []
    try:
        import ast
        cond_vars = set()
        if condition:
            try:
                tree = ast.parse(condition)
                cond_vars = set(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))
            except Exception:
                pass

        key_indicators = [
            'AI_SCORE', 'feature_time', 'current_price', 'buy_price', 'price_change_pct', 'current_profit_pct',
            'from_peak_pct', 'highest_price', 'tick_strength', 'market_kosdaq_roc',
            'tick_rsi21', 'tick_macd_hist', 'tick_disparity20', 'tick_bb_position',
            'tick_velocity', 'tick_price_roc', 'tick_vol_roc'
        ]
        
        target_vars = list(dict.fromkeys(list(cond_vars) + key_indicators))
        
        log_lines.append(f"📊 [{code}] '{strategy_name}' ({strategy_type}) 판단 근거 지표 데이터:")
        for var in target_vars:
            if var in safe_locals:
                val = safe_locals[var]
                if type(val).__name__ in ('ndarray', 'Series'):
                    val = val.tolist()
                if isinstance(val, (list, tuple)):
                    last_val = val[-1] if len(val) > 0 else 0
                    if isinstance(last_val, float):
                        log_lines.append(f"  - {var}: {last_val:.4f}")
                    else:
                        log_lines.append(f"  - {var}: {last_val}")
                else:
                    if isinstance(val, float):
                        log_lines.append(f"  - {var}: {val:.4f}")
                    else:
                        log_lines.append(f"  - {var}: {val}")
    except Exception as ex:
        pass
    return log_lines

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
        
        # KOSDAQ 지수 데이터 로드 및 병합 (stock_data에 이미 유효한 market_kosdaq_roc 수치가 없는 경우에만 진행)
        has_existing_roc = 'market_kosdaq_roc' in df.columns and (df['market_kosdaq_roc'].fillna(0.0) != 0.0).any()
        
        if has_existing_roc:
            # stock_data DB에 이미 실전 수집된 market_kosdaq_roc 값이 있으므로 기존 값 보존
            df['market_kosdaq_roc'] = df['market_kosdaq_roc'].fillna(0.0)
            df['market_kosdaq_roc'] = np.where(np.abs(df['market_kosdaq_roc']) > 0.5,
                                              df['market_kosdaq_roc'] / 100.0,
                                              df['market_kosdaq_roc'])
        else:
            try:
                conn = sqlite3.connect(self.db_path)
                kosdaq_query = "SELECT datetime as kosdaq_time, close as kosdaq_close, open as kosdaq_open FROM kosdaq_3m"
                kosdaq_df = pd.read_sql(kosdaq_query, conn)
                conn.close()
                
                if not kosdaq_df.empty:
                    df['dt_obj'] = pd.to_datetime(df['datetime'], errors='coerce')
                    kosdaq_df['dt_obj'] = pd.to_datetime(kosdaq_df['kosdaq_time'], errors='coerce')
                    
                    kosdaq_df = kosdaq_df.sort_values('dt_obj').dropna(subset=['dt_obj'])
                    
                    kosdaq_df['date'] = kosdaq_df['dt_obj'].dt.date
                    daily_open = kosdaq_df.groupby('date')['kosdaq_open'].transform('first')
                    kosdaq_df['market_kosdaq_roc'] = np.where(daily_open > 0, 
                                                            (kosdaq_df['kosdaq_close'] - daily_open) / daily_open, 
                                                            0.0)
                    kosdaq_df['market_kosdaq_roc'] = np.where(np.abs(kosdaq_df['market_kosdaq_roc']) > 0.5,
                                                             kosdaq_df['market_kosdaq_roc'] / 100.0,
                                                             kosdaq_df['market_kosdaq_roc'])
                    
                    df = df.sort_values('dt_obj').dropna(subset=['dt_obj'])
                    
                    # 기존 market_kosdaq_roc 컬럼 충돌 방지를 위해 이전에 있던 컬럼 드롭 후 병합
                    if 'market_kosdaq_roc' in df.columns:
                        df.drop(columns=['market_kosdaq_roc'], inplace=True)
                        
                    df = pd.merge_asof(df, kosdaq_df[['dt_obj', 'market_kosdaq_roc']], on='dt_obj', direction='backward')
                    df['market_kosdaq_roc'] = df['market_kosdaq_roc'].fillna(0.0)
                    df['market_kosdaq_roc'] = np.where(np.abs(df['market_kosdaq_roc']) > 0.5,
                                                      df['market_kosdaq_roc'] / 100.0,
                                                      df['market_kosdaq_roc'])
                    df = df.sort_values('datetime')
                    df.drop(columns=['dt_obj'], inplace=True)
                    
                    valid_kosdaq_cnt = (df['market_kosdaq_roc'] != 0.0).sum()
                    if valid_kosdaq_cnt == 0:
                        logger.warning("⚠️ 백테스트 기간 내 KOSDAQ 지수(kosdaq_3m) 데이터가 누락되어 market_kosdaq_roc가 모두 0.0으로 설정되었습니다. DB 지수 데이터를 최신화해 주세요.")
                else:
                    logger.warning("⚠️ kosdaq_3m 테이블이 비어있어 market_kosdaq_roc가 0.0으로 설정되었습니다.")
                    df['market_kosdaq_roc'] = 0.0
            except Exception as e:
                logger.error(f"KOSDAQ 병합 중 오류: {e}")
                df['market_kosdaq_roc'] = 0.0
        
        return df



    def run(self, start_date, end_date, code='ALL', progress_callback=None, custom_buy=None, custom_sell=None, initial_capital=10000000, buycount=3):
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
            capital = initial_capital
            invested_per_trade = capital / max(1, buycount) # buycount 등분 분할 투자
            
            max_capital = capital
            mdd = 0.0
            eval_errors = 0
            debug_logs = []

            # --- 초기 설정 로그 ---
            debug_logs.append(f"📊 [시작] 백테스트 기간: {start_date} ~ {end_date}")
            debug_logs.append(f"💰 초기 자본금: {initial_capital:,.0f}원 | 최대 보유 종목: {buycount}개 | 종목당 투자금: {invested_per_trade:,.0f}원")
            debug_logs.append(f"📈 매수 전략 {len(buy_strategies)}개: {', '.join([s.get('name','?') for s in buy_strategies])}")
            debug_logs.append(f"📉 매도 전략 {len(sell_strategies)}개: {', '.join([s.get('name','?') for s in sell_strategies])}")
            debug_logs.append(f"🔍 분석 대상 종목: {total_codes}개 | 총 틱 데이터: {len(df):,}건")

            from strategy_utils import KiwoomIndicatorExtractor, LGBM_MODEL
            
            # AI_SCORE 사용 여부 판단
            uses_ai = False
            for stg in buy_strategies + sell_strategies:
                if 'AI_SCORE' in stg.get('content', ''):
                    uses_ai = True
                    break
            
            if uses_ai:
                debug_logs.append(f"🤖 AI_SCORE 사용 감지 - LightGBM 모델 상태: {'✅ 로드됨' if LGBM_MODEL else '❌ 미로드 (AI_SCORE=0.0)'}")
            else:
                debug_logs.append(f"ℹ️ AI_SCORE 미사용 전략")
            debug_logs.append("─" * 50)
            
            
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
                    sell_ratio_val = float(stg.get('partial_sell_ratio', stg.get('sell_ratio', stg.get('ratio', 1.0))))
                    sell_compiled.append((stg['name'], sell_ratio_val, compile(stg['content'], '<string>', 'eval')))
                except Exception as e:
                    logger.error(f"매도 로직 컴파일 오류 ({stg['name']}): {e}")

            # --- Phase 1: Precomputation ---
            processed_dfs = []
            stock_data = {}
            
            for current_code, group_df in grouped:
                code_idx += 1
                group_df = group_df.reset_index(drop=True)
                
                # 중복 컬럼 제거 (LEFT JOIN 시 t.* 와 m.* 이름이 중복되는 경우 대비)
                group_df = group_df.loc[:, ~group_df.columns.duplicated(keep='last')]
                
                first_eval_logged = False
                
                # 1. 컬럼명 일괄 변경
                group_df = group_df.rename(columns={
                    'tick_open': 'open', 'tick_high': 'high', 'tick_low': 'low', 
                    'tick_close': 'close', 'tick_volume': 'volume'
                })
                
                # 1.5 3분봉 데이터 존재 확인
                try:
                    group_df['datetime'] = pd.to_datetime(group_df['datetime'])
                    # 3분봉 식별을 위한 고유 period_id 생성 (빠른 배열 추출용)
                    group_df['period_id'] = (group_df['datetime'].dt.minute // 3) + group_df['datetime'].dt.hour * 20 + group_df['datetime'].dt.dayofyear * 1000
                    
                    has_db_min3 = all(col in group_df.columns for col in ['min3_open', 'min3_close', 'min3_volume'])
                    if not has_db_min3 or group_df['min3_volume'].notna().sum() == 0:
                        logger.warning(f"{current_code}: DB에 3분봉 데이터가 불충분합니다. 지표 생성 없이 진행합니다.")
                except Exception as e:
                    logger.error(f"3분봉 데이터 확인 오류 ({current_code}): {e}")
                    
                # 감시 이력 기반 필터링 (IS_MONITORED)
                # 데이터베이스(stock_data)에 저장된 데이터 자체가 곧 감시 대상이었음을 의미하므로 
                # 모든 로우에 대해 IS_MONITORED = True 로 일괄 적용합니다.
                group_df['IS_MONITORED'] = True
                        
                # 2. DB 지표를 엔진 변수명으로 매핑 (재계산 방지)
                try:
                    # 틱 지표 및 분봉 지표 매핑 (tick_ma5 -> MA5, min3_rsi -> MIN3_RSI 등)
                    for col in group_df.columns:
                        if col.startswith('tick_'):
                            upper_name = col[4:].upper()
                            group_df[upper_name] = group_df[col]
                        elif col.startswith('min3_'):
                            upper_name = col.upper()
                            group_df[upper_name] = group_df[col]
                            
                    # 특수 지표 변수명 명시적 매핑
                    if 'tick_velocity' in group_df.columns:
                        group_df['TICK_VELOCITY'] = group_df['tick_velocity']
                    if 'tick_relative_position' in group_df.columns:
                        group_df['RELATIVE_POSITION'] = group_df['tick_relative_position']
                except Exception as e:
                    logger.error(f"지표 매핑 오류 ({current_code}): {e}")
                    
                n = len(group_df)
                # DB에 이미 수집/저장된 ai_score 컬럼이 존재하면 그대로 사용
                if 'ai_score' in group_df.columns and (group_df['ai_score'].fillna(0.0) != 0.0).any():
                    group_df['AI_SCORE'] = group_df['ai_score'].fillna(0.0)
                elif 'AI_SCORE' in group_df.columns and (group_df['AI_SCORE'].fillna(0.0) != 0.0).any():
                    group_df['AI_SCORE'] = group_df['AI_SCORE'].fillna(0.0)
                else:
                    group_df['AI_SCORE'] = 0.0
                
                # 3. AI_SCORE 배치 계산 (DB 수치가 없거나 0일 때만 재계산)
                if uses_ai and (group_df['AI_SCORE'] == 0.0).all() and LGBM_MODEL:
                    try:
                        # 3.0 누락된 파생 지표(VWAP 등) 백필 계산 (DB의 tick_close, tick_volume 사용)
                        if 'tick_VWAP' not in group_df.columns:
                            try:
                                if 'high' in group_df.columns and 'low' in group_df.columns:
                                    typ = (group_df['high'] + group_df['low'] + group_df['close']) / 3
                                else:
                                    typ = group_df['close']
                                vol = group_df['volume'] if 'volume' in group_df.columns else np.ones(n)
                                VWAP_WINDOW = 60
                                rolling_pv = pd.Series(typ * vol).rolling(VWAP_WINDOW, min_periods=1).sum().values
                                rolling_v = pd.Series(vol).rolling(VWAP_WINDOW, min_periods=1).sum().values
                                group_df['tick_VWAP'] = np.where(rolling_v > 0, rolling_pv / rolling_v, group_df['tick_close'])
                            except Exception as e:
                                logger.error(f"VWAP 계산 오류 ({current_code}): {e}")
                                group_df['tick_VWAP'] = group_df['close']

                        f_strength = group_df['tick_strength'].values if 'tick_strength' in group_df.columns else np.zeros(n)
                        f_raw_velocity = group_df['tick_velocity'].values if 'tick_velocity' in group_df.columns else np.full(n, 999999.0)
                        f_velocity = np.log1p(np.maximum(0, f_raw_velocity))
                        f_relative = group_df['min3_relative_position'].values if 'min3_relative_position' in group_df.columns else np.zeros(n)
                        
                        vol = group_df['tick_volume'].values if 'tick_volume' in group_df.columns else np.ones(n)
                        f_ma_ratio = np.zeros(n)
                        if n > 0:
                            roll_avg = pd.Series(vol).rolling(window=20, min_periods=1).mean().values
                            roll_avg = np.where(roll_avg == 0, 1, roll_avg)
                            f_ma_ratio = vol / roll_avg

                        if 'tick_vwap_distance' in group_df.columns and group_df['tick_vwap_distance'].notna().sum() > 0:
                            f_vwap_dist = group_df['tick_vwap_distance'].fillna(0.0).values
                        else:
                            close_vals = group_df['close'].values if 'close' in group_df.columns else np.zeros(n)
                            vwap_vals = group_df['tick_VWAP'].values if 'tick_VWAP' in group_df.columns else close_vals.copy()
                            vwap_safe = np.where(vwap_vals == 0, 1e-9, vwap_vals)
                            f_vwap_dist = (close_vals - vwap_vals) / vwap_safe
                        
                        f_macd_hist = group_df['tick_macd_hist'].values if 'tick_macd_hist' in group_df.columns else np.zeros(n)
                        
                        # DB에 저장된 온전한 RSI21 컬럼 최우선 사용
                        if 'tick_rsi21' in group_df.columns:
                            f_rsi = group_df['tick_rsi21'].values
                        else:
                            f_rsi = np.full(n, 50.0)
                        
                        dt_series = pd.to_datetime(group_df['datetime'], errors='coerce')
                        f_time = np.clip((dt_series.dt.hour * 60 + dt_series.dt.minute).values - 540, 0, 390)
                        
                        # [DB 직결] 파생 가속도 지표 (price_roc, vol_roc) 및 돌파 가속도 (impulse) DB 컬럼 최우선 사용
                        if 'tick_price_roc' in group_df.columns and group_df['tick_price_roc'].notna().sum() > 0:
                            f_price_roc = group_df['tick_price_roc'].fillna(0.0).values
                        elif 'close' in group_df.columns:
                            f_price_roc = group_df['close'].pct_change(periods=10).fillna(0.0).values
                        else:
                            f_price_roc = np.zeros(n)
                            
                        if 'tick_impulse' in group_df.columns and group_df['tick_impulse'].notna().sum() > 0:
                            f_impulse = group_df['tick_impulse'].fillna(0.0).values
                        else:
                            f_impulse = f_velocity * f_price_roc

                        # [DB 직결] ATR 변동성 비율 (f_atr_ratio) DB 컬럼 최우선 사용
                        if 'tick_atr_ratio' in group_df.columns and group_df['tick_atr_ratio'].notna().sum() > 0:
                            f_atr_ratio = group_df['tick_atr_ratio'].fillna(0.0).values
                        elif 'high' in group_df.columns and 'low' in group_df.columns and 'close' in group_df.columns:
                            tr1 = group_df['high'] - group_df['low']
                            prev_c = group_df['close'].shift(1).fillna(group_df['open'] if 'open' in group_df.columns else group_df['close'])
                            tr2 = (group_df['high'] - prev_c).abs()
                            tr3 = (group_df['low'] - prev_c).abs()
                            tr_df = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                            atr20 = tr_df.rolling(20, min_periods=1).mean().values
                            c_safe = np.where(group_df['close'] == 0, 1e-9, group_df['close'])
                            f_atr_ratio = np.nan_to_num((atr20 / c_safe) * 100.0, nan=0.0)
                        else:
                            f_atr_ratio = np.zeros(n)

                        # [DB 직결] 거래량 변화율 (f_vol_roc) DB 컬럼 최우선 사용
                        if 'tick_vol_roc' in group_df.columns and group_df['tick_vol_roc'].notna().sum() > 0:
                            f_vol_roc = group_df['tick_vol_roc'].fillna(1.0).values
                        elif 'volume' in group_df.columns:
                            vol_sum_5 = group_df['volume'].rolling(5).sum()
                            prev_vol_sum_5 = vol_sum_5.shift(5)
                            f_vol_roc = np.where(prev_vol_sum_5 > 0, vol_sum_5 / prev_vol_sum_5, 1.0)
                            f_vol_roc = pd.Series(f_vol_roc).fillna(1.0).values
                        else:
                            f_vol_roc = np.ones(n)
                        
                        num_features = LGBM_MODEL.num_feature()
                        
                        if num_features >= 14:
                            if 'min3_ma5' in group_df.columns and 'min3_ma20' in group_df.columns:
                                f_min3_trend_agree = ((group_df['min3_ma5'] > group_df['min3_ma20']) & (group_df['min3_ma20'] > 0)).astype(int).values
                            else:
                                f_min3_trend_agree = np.zeros(n)
                                
                            if 'tick_ma_spread' in group_df.columns and group_df['tick_ma_spread'].notna().sum() > 0:
                                f_tick_ma_spread = group_df['tick_ma_spread'].fillna(0.0).values
                            elif 'tick_ma5' in group_df.columns and 'tick_ma20' in group_df.columns:
                                ma20_safe = np.where(group_df['tick_ma20'] == 0, 1e-9, group_df['tick_ma20'])
                                f_tick_ma_spread = ((group_df['tick_ma5'] - group_df['tick_ma20']) / ma20_safe).fillna(0.0).values
                            else:
                                f_tick_ma_spread = np.zeros(n)
                                
                            if 'close' in group_df.columns and 'volume' in group_df.columns:
                                amount = group_df['close'] * group_df['volume']
                                roll_amt = amount.rolling(10).mean().shift(1).fillna(1e-9).values
                                roll_amt = np.where(roll_amt == 0, 1e-9, roll_amt)
                                f_tic_amount_spike = (amount.values / roll_amt)
                                f_tic_amount_spike = np.nan_to_num(f_tic_amount_spike, nan=0.0, posinf=0.0, neginf=0.0)
                            else:
                                f_tic_amount_spike = np.zeros(n)
                                
                            if 'tick_tail_ratio' in group_df.columns and group_df['tick_tail_ratio'].notna().sum() > 0:
                                f_tic_tail_ratio = group_df['tick_tail_ratio'].fillna(0.0).values
                            elif 'high' in group_df.columns and 'low' in group_df.columns and 'close' in group_df.columns:
                                hl_diff = group_df['high'] - group_df['low']
                                hl_safe = np.where(hl_diff <= 0, 1e-9, hl_diff)
                                f_tic_tail_ratio = np.where(hl_diff > 0, ((group_df['high'] - group_df['close']) / hl_safe), 0.0)
                                f_tic_tail_ratio = pd.Series(f_tic_tail_ratio).fillna(0.0).values
                            else:
                                f_tic_tail_ratio = np.zeros(n)
                                
                        if num_features >= 15:
                            f_buy_sell_ratio = np.where(vol > 0, group_df['tick_buy_volume'].values / vol, 0.5) if 'tick_buy_volume' in group_df.columns else np.full(n, 0.5)
                            
                            if 'tick_spread' in group_df.columns and group_df['tick_spread'].notna().sum() > 0:
                                f_spread = group_df['tick_spread'].fillna(0.0).values
                            elif 'high' in group_df.columns and 'low' in group_df.columns and 'close' in group_df.columns:
                                close_safe = np.where(group_df['close'] == 0, 1e-9, group_df['close'])
                                f_spread = ((group_df['high'] - group_df['low']) / close_safe).fillna(0.0).values
                            else:
                                f_spread = np.zeros(n)
                                
                            if 'tick_disparity20' in group_df.columns and group_df['tick_disparity20'].notna().sum() > 0:
                                f_disparity = group_df['tick_disparity20'].fillna(100.0).values
                            elif 'close' in group_df.columns and 'tick_ma20' in group_df.columns:
                                ma20_safe = np.where(group_df['tick_ma20'] == 0, 1e-9, group_df['tick_ma20'])
                                f_disparity = ((group_df['close'] / ma20_safe) * 100).fillna(100.0).values
                            else:
                                f_disparity = np.full(n, 100.0)

                            if 'tick_bb_position' in group_df.columns and group_df['tick_bb_position'].notna().sum() > 0:
                                f_bb_pos = group_df['tick_bb_position'].fillna(0.5).values
                            elif 'close' in group_df.columns and 'tick_ma20' in group_df.columns:
                                std20 = group_df['close'].rolling(20, min_periods=1).std(ddof=1).fillna(0).values
                                bb_upper = group_df['tick_ma20'].values + (2 * std20)
                                bb_lower = group_df['tick_ma20'].values - (2 * std20)
                                bb_diff = bb_upper - bb_lower
                                bb_diff_safe = np.where(bb_diff <= 0, 1e-9, bb_diff)
                                f_bb_pos = np.where(bb_diff > 0, (group_df['close'].values - bb_lower) / bb_diff_safe, 0.5)
                            else:
                                f_bb_pos = np.full(n, 0.5)

                            f_imbalance = group_df['tick_imbalance'].fillna(0.5).values if 'tick_imbalance' in group_df.columns else np.full(n, 0.5)
                            f_market_kosdaq_roc = group_df['market_kosdaq_roc'].fillna(0.0).values if 'market_kosdaq_roc' in group_df.columns else np.zeros(n)
                            
                        if num_features == 17:
                            mat = np.column_stack((
                                f_strength, f_velocity, f_relative, f_ma_ratio,
                                f_vwap_dist, f_macd_hist, f_rsi,
                                f_price_roc, f_vol_roc,
                                f_tick_ma_spread, f_tic_tail_ratio, f_buy_sell_ratio, f_spread,
                                f_disparity, f_bb_pos, f_imbalance, f_market_kosdaq_roc
                            ))
                        elif num_features == 16:
                            # 최신 16개 피처 (tick_impulse 및 tick_atr_ratio 포함)
                            mat = np.column_stack((
                                f_strength, f_velocity, f_relative,
                                f_vwap_dist, f_macd_hist, f_rsi,
                                f_price_roc, f_impulse, f_atr_ratio,
                                f_tick_ma_spread, f_tic_tail_ratio, f_spread,
                                f_disparity, f_bb_pos, f_imbalance, f_market_kosdaq_roc
                            ))
                        elif num_features == 15:
                            # 최신 15개 피처 (노이즈 피처 제거, tick_impulse 돌파가속도 추가)
                            mat = np.column_stack((
                                f_strength, f_velocity, f_relative,
                                f_vwap_dist, f_macd_hist, f_rsi,
                                f_price_roc, f_impulse,
                                f_tick_ma_spread, f_tic_tail_ratio, f_spread,
                                f_disparity, f_bb_pos, f_imbalance, f_market_kosdaq_roc
                            ))
                        elif num_features == 14:
                            # 14차원 구버전 피처
                            f_buy_sell_ratio = np.where(vol > 0, group_df['buy_volume'].values / vol, 0.5) if 'buy_volume' in group_df.columns else np.full(n, 0.5)
                            if 'high' in group_df.columns and 'low' in group_df.columns and 'close' in group_df.columns:
                                close_safe = np.where(group_df['close'] == 0, 1e-9, group_df['close'])
                                f_spread = ((group_df['high'] - group_df['low']) / close_safe).fillna(0.0).values
                            else:
                                f_spread = np.zeros(n)
                            mat = np.column_stack((
                                f_strength, f_velocity, f_relative, f_ma_ratio,
                                f_vwap_dist, f_macd_hist, f_rsi, f_time,
                                f_price_roc, f_vol_roc,
                                f_tick_ma_spread, f_tic_tail_ratio, f_buy_sell_ratio, f_spread
                            ))
                        elif num_features == 10:
                            mat = np.column_stack((
                                f_strength, f_velocity, f_relative, f_ma_ratio,
                                f_vwap_dist, f_macd_hist, f_rsi, f_time,
                                f_price_roc, f_vol_roc
                            ))
                        elif num_features == 8:
                            mat = np.column_stack((f_strength, f_velocity, f_relative, f_ma_ratio, f_vwap_dist, f_macd_hist, f_rsi, f_time))
                        elif num_features == 7:
                            mat = np.column_stack((f_strength, f_velocity, f_relative, f_ma_ratio, f_vwap_dist, f_macd_hist, f_rsi))
                        elif num_features == 4:
                            mat = np.column_stack((f_strength, f_velocity, f_relative, f_ma_ratio))

                        else:
                            logger.warning(f"⚠️ 백테스터에 {num_features}개 피처에 대한 행렬 매핑 로직이 구현되지 않았습니다. 기본 0.0 값으로 평가됩니다.")
                            mat = np.zeros((n, num_features))
                        
                        group_df['AI_SCORE'] = LGBM_MODEL.predict(mat, num_threads=1)
                    except Exception as e:
                        logger.error(f"AI_SCORE 배치 계산 오류 ({current_code}): {e}")
                        debug_logs.append(f"⚠️ [{current_code}] AI_SCORE 계산 오류: {e}")
                
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
            daily_peak_prices = {} # {code: peak_high_price}
            
            for current_time, time_df in grouped_by_time:
                date_str = str(current_time)[:10]
                if current_date != date_str:
                    if current_date is not None and portfolio:
                        debug_logs.append(f"⚠️ [{current_date}] 일일 데이터 종료로 인한 잔여 포지션 {len(portfolio)}개 강제 청산 (오버나잇 방지)")
                        for p_code, pos in list(portfolio.items()):
                            sd = stock_data[p_code]
                            safe_idx = max(0, sd['current_idx'] - 1)
                            last_price = float(sd['precomputed']['close'][safe_idx])
                            last_time = str(sd['precomputed']['datetime'][safe_idx]).replace('T', ' ')
                            
                            sell_qty = pos['qty']
                            trade_profit = (last_price - pos['buy_price']) * sell_qty
                            trade_profit -= (pos['buy_price'] * sell_qty * 0.009)
                            total_profit += trade_profit
                            capital += trade_profit
                            if trade_profit > 0: win_count += 1
                            else: loss_count += 1
                            real_profit_pct = (last_price - pos['buy_price']) / pos['buy_price'] * 100.0 - 0.9
                            
                            profit_emoji = '🟢' if trade_profit >= 0 else '🔴'
                            debug_logs.append(f"{profit_emoji} [{p_code}] 데이터마감 강제청산 | {pos['buy_price']:,.0f}→{last_price:,.0f} ({real_profit_pct:+.2f}%) | 손익: {trade_profit:+,.0f}원")
                            
                            trades.append({
                                'code': p_code,
                                'buy_time': pos['buy_time'],
                                'sell_time': last_time,
                                'buy_price': pos['buy_price'],
                                'sell_price': last_price,
                                'qty': sell_qty,
                                'profit_pct': real_profit_pct,
                                'profit_amount': trade_profit,
                                'buy_stg': pos['stg'],
                                'sell_stg': "데이터마감 강제청산"
                            })
                            max_capital = max(max_capital, capital)
                            mdd = max(mdd, (max_capital - capital) / max_capital * 100.0)
                        portfolio.clear()
                        
                    current_date = date_str
                    daily_blacklist.clear()
                    cooldown_list.clear()
                    daily_peak_prices.clear()
                    
                time_idx += 1
                if progress_callback and time_idx % max(1, total_times // 20) == 0:
                    prog = 30 + int((time_idx / total_times) * 60)
                    progress_callback(prog, f"시뮬레이션 진행 중... ({time_idx}/{total_times}) 틱")
                
                current_time_str = str(current_time).replace('T', ' ')
                time_part = current_time_str[11:16].replace(":", "") if len(current_time_str) >= 16 else ""
                is_market_close = (time_part >= "1518")
                is_buy_blocked_time = (time_part >= buy_end_time_str)
                is_force_sell_time = sell_all_enabled and (time_part >= sell_all_time_str)
                
                # [당일 고점 및 -10% 폭락 블랙리스트 업데이트]
                for _, r in time_df.iterrows():
                    c_code = r['code']
                    c_high = float(r['high']) if 'high' in r else float(r['close'])
                    c_close = float(r['close'])
                    
                    if c_code not in daily_peak_prices:
                        daily_peak_prices[c_code] = c_high
                    else:
                        daily_peak_prices[c_code] = max(daily_peak_prices[c_code], c_high)
                        
                    # 최고가 대비 10% 이상 폭락 시 당일 블랙리스트 추가 (모멘텀 상실)
                    if daily_peak_prices[c_code] > 0 and c_close <= daily_peak_prices[c_code] * 0.90:
                        if c_code not in daily_blacklist:
                            daily_blacklist.add(c_code)
                
                # 1. 매도 평가 (현재 보유 종목 중 time_df에 존재하는 것)
                for _, row in time_df.iterrows():
                    current_code = row['code']
                    if current_code in portfolio:
                        sd = stock_data[current_code]
                        idx = sd['current_idx']
                        open_p = float(row['open'])
                        high_p = float(row['high'])
                        low_p = float(row['low'])
                        close_p = float(row['close'])

                        # 양봉/음봉에 따른 시뮬레이션 가격 경로 설정
                        if close_p >= open_p:
                            price_path = [open_p, low_p, high_p, close_p]
                        else:
                            price_path = [open_p, high_p, low_p, close_p]
                            
                        pos = portfolio[current_code]
                        buy_price = pos['buy_price']
                        
                        # eval 호환성 base_locals_dict 구성 (현재 봉 기준, 한 번만 생성)
                        start_idx = max(0, idx - 300)
                        base_locals_dict = {}
                        
                        keep_indices = None
                        if 'period_id' in sd['precomputed']:
                            p_ids = sd['precomputed']['period_id'][start_idx:idx] # Future Leak 차단을 위해 idx까지만 자름 (이전 완성봉)
                            if len(p_ids) > 1:
                                changes = p_ids[:-1] != p_ids[1:]
                                keep_indices = np.append(changes, True)
                            elif len(p_ids) == 1:
                                keep_indices = np.array([True])
                            else:
                                keep_indices = np.array([])
                                
                        for col_name, col_arr in sd['precomputed'].items():
                            arr_slice = col_arr[start_idx:idx] # 이전 완성봉 데이터까지만 복사
                            if col_name.startswith('min3_') and keep_indices is not None and len(keep_indices) > 0:
                                arr_slice = arr_slice[keep_indices]
                            base_locals_dict[col_name] = arr_slice
                            if not col_name.startswith('tick_') and not col_name.startswith('min3_'):
                                base_locals_dict[f'tick_{col_name}'] = arr_slice
                                
                        if 'AI_SCORE' in sd['precomputed'] and idx > 0:
                            base_locals_dict['AI_SCORE'] = float(sd['precomputed']['AI_SCORE'][idx-1])
                        else:
                            base_locals_dict['AI_SCORE'] = 0.0
                            
                        if 'market_kosdaq_roc' in sd['precomputed'] and idx > 0:
                            raw_k = float(sd['precomputed']['market_kosdaq_roc'][idx-1])
                            base_locals_dict['market_kosdaq_roc'] = raw_k / 100.0 if abs(raw_k) > 0.5 else raw_k
                        else:
                            base_locals_dict['market_kosdaq_roc'] = 0.0

                        # --- 누락된 파생 지표 동기화 (strategy_utils.py 와 동일하게 구성) ---
                        roc_array = base_locals_dict.get('ROC', [])
                        base_locals_dict['ROC_recent'] = roc_array[-30:].tolist() if len(roc_array) > 0 else []
                        
                        high_array = base_locals_dict.get('high', [])
                        if len(high_array) > 1:
                            recent_highs = high_array[-30:-1]
                            if len(recent_highs) > 0:
                                highest_recent = np.max(recent_highs)
                                current_close = base_locals_dict.get('close', [0])[-1]
                                base_locals_dict['is_pullback'] = bool(current_close < highest_recent)
                            else:
                                base_locals_dict['is_pullback'] = False
                        else:
                            base_locals_dict['is_pullback'] = False
                            
                        vol_array = base_locals_dict.get('volume', [])
                        if len(vol_array) > 0:
                            base_locals_dict['avg_volume'] = float(np.mean(vol_array))
                            base_locals_dict['volume_ratio'] = float(vol_array[-1]) / base_locals_dict['avg_volume'] if base_locals_dict['avg_volume'] > 0 else 1.0
                            base_locals_dict['tick_avg_volume_10'] = float(np.mean(vol_array[-10:]))
                            base_locals_dict['tick_avg_volume_5'] = float(np.mean(vol_array[-5:]))
                            ma_20_vol = np.mean(vol_array[-20:])
                            base_locals_dict['tick_volume_ma_ratio'] = float(vol_array[-1]) / ma_20_vol if ma_20_vol > 0 else 0.0
                        else:
                            base_locals_dict['avg_volume'] = 0.0
                            base_locals_dict['volume_ratio'] = 1.0
                            base_locals_dict['tick_avg_volume_10'] = 0.0
                            base_locals_dict['tick_avg_volume_5'] = 0.0
                            base_locals_dict['tick_volume_ma_ratio'] = 0.0
                            
                        base_locals_dict['ORDER_BOOK_IMBALANCE'] = np.array([0.0])
                        base_locals_dict['tick_order_book_imbalance'] = np.array([0.0])
                            
                        sell_signal = False
                        sell_ratio = 1.0
                        matched_sell_stg = ""
                        final_sim_price = close_p
                        final_profit_pct = 0.0
                        
                        # OHLC 보간법: 캔들 내부 가격 경로를 순회하며 매도 룰 평가
                        for sim_price in price_path:
                            current_price = sim_price
                            
                            # highest_price 갱신
                            if current_price > pos.get('highest_price', buy_price):
                                pos['highest_price'] = current_price
                                
                            highest_price = pos['highest_price']
                            from_peak_pct = (current_price - highest_price) / highest_price * 100.0 if highest_price > 0 else 0.0
                            raw_price_change_pct = (current_price - buy_price) / buy_price * 100.0 if buy_price > 0 else 0.0
                            real_profit_pct = raw_price_change_pct - 0.8
                            
                            locals_dict = base_locals_dict.copy()
                            locals_dict['code'] = current_code
                            dt_obj = pd.to_datetime(row['datetime'])
                            locals_dict['datetime'] = dt_obj
                            locals_dict['feature_time'] = max(0, min(390, (dt_obj.hour * 60 + dt_obj.minute) - (9 * 60)))
                            locals_dict['current_price'] = current_price
                            locals_dict['price_change_pct'] = raw_price_change_pct
                            locals_dict['profit_pct'] = real_profit_pct
                            locals_dict['current_profit_pct'] = real_profit_pct
                            locals_dict['buy_price'] = buy_price
                            locals_dict['buy_time'] = pos['buy_time']
                            locals_dict['holding_amount'] = buy_price * pos['qty']
                            locals_dict['highest_price'] = highest_price
                            locals_dict['from_peak_pct'] = from_peak_pct
                            
                            for stg_name_str, stg_ratio, stg_code in sell_compiled:
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
                                        debug_logs.append(f"❌ [{current_code}] 매도 평가 오류 ({stg_name_str}): {e}")
                                        
                            if sell_signal:
                                final_sim_price = current_price
                                final_profit_pct = real_profit_pct
                                break # 매도 조건 달성 시, 남은 가격 경로는 건너뜀
                        
                        # 오버나잇 방지 강제 청산 및 장마감 강제 청산 (종가 기준)
                        if not sell_signal and (is_force_sell_time or is_market_close):
                            sell_signal = True
                            sell_ratio = 1.0
                            matched_sell_stg = "마감 강제청산"
                            final_sim_price = close_p
                            final_profit_pct = (final_sim_price - buy_price) / buy_price * 100.0 - 0.9
                            
                        # 루프 종료 후, 실제로 체결된 가격으로 변수 복구
                        current_price = final_sim_price
                        real_profit_pct = final_profit_pct
                            
                        if sell_signal:
                            sell_qty = int(pos['qty'] * sell_ratio)
                            if sell_qty > 0:
                                trade_profit = (current_price - buy_price) * sell_qty
                                trade_profit -= (buy_price * sell_qty * 0.009)
                                total_profit += trade_profit
                                capital += trade_profit
                                if trade_profit > 0: win_count += 1
                                else: loss_count += 1
                                
                                debug_logs.append(f"[SELL_SIGNAL] 📉 [{current_code}] 매도 '{matched_sell_stg}' 조건 충족!")
                                matched_cond = next((code_str for name_str, _, code_str in sell_compiled if name_str == matched_sell_stg), "")
                                ind_lines = format_backtest_indicator_log(current_code, matched_sell_stg, "매도", matched_cond, locals_dict)
                                for line in ind_lines:
                                    debug_logs.append(line)
                                
                                profit_emoji = '🟢' if trade_profit >= 0 else '🔴'
                                debug_logs.append(f"{profit_emoji} [{current_time_str[5:16]}] 매도 {current_code} | '{matched_sell_stg}' | {buy_price:,.0f}→{current_price:,.0f} ({real_profit_pct:+.2f}%) | 손익: {trade_profit:+,.0f}원 | 잔고: {capital:,.0f}원")
                                
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
                                        from datetime import timedelta
                                        cooldown_list[current_code] = current_time + timedelta(minutes=15) # 손절 15분 쿨타임 완화
                                        debug_logs.append(f"   ⏳ [{current_code}] 손절 → 15분 쿨타임 적용")
                                    else:
                                        from datetime import timedelta
                                        cooldown_list[current_code] = current_time + timedelta(minutes=15) # 익절 15분 쿨타임 완화
                                        debug_logs.append(f"   ⏳ [{current_code}] 익절 → 15분 쿨타임 적용")
                                else:
                                    portfolio[current_code]['qty'] -= sell_qty
                                    portfolio[current_code].setdefault('executed_sell_rules', set()).add(matched_sell_stg)
                                    debug_logs.append(f"   📌 [{current_code}] 분할매도 ({int(sell_ratio*100)}%) → 잔여 {portfolio[current_code]['qty']}주")
                                    
                # 2. 매수 평가 (보유 슬롯이 비어있고 매수 차단 시간이 아닐 때만)
                if not is_market_close and not is_buy_blocked_time:
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
                        
                        # 감시 시작 1번째 60틱봉부터 즉시 매수 평가 (실전 매매와 100% 동일하게 일치)
                        if idx >= 0 and current_code not in portfolio and current_code not in daily_blacklist:
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
                                
                                # 빠른 3분봉 배열 추출을 위한 인덱스 마스크 계산
                                keep_indices = None
                                if 'period_id' in sd['precomputed']:
                                    p_ids = sd['precomputed']['period_id'][start_idx:idx+1]
                                    if len(p_ids) > 1:
                                        changes = p_ids[:-1] != p_ids[1:]
                                        keep_indices = np.append(changes, True)
                                    else:
                                        keep_indices = np.array([True])
                                        
                                for col_name, col_arr in sd['precomputed'].items():
                                    arr_slice = col_arr[start_idx:idx+1]
                                    
                                    if col_name.startswith('min3_') and keep_indices is not None:
                                        # 진짜 3분봉 배열(완성된 3분봉들 + 현재 진행중인 3분봉)로 압축
                                        arr_slice = arr_slice[keep_indices]
                                        
                                    locals_dict[col_name] = arr_slice
                                    if not col_name.startswith('tick_') and not col_name.startswith('min3_'):
                                        locals_dict[f'tick_{col_name}'] = arr_slice
                                        
                                if 'AI_SCORE' in sd['precomputed']:
                                    locals_dict['AI_SCORE'] = float(sd['precomputed']['AI_SCORE'][idx])
                                
                                # market_kosdaq_roc도 스칼라 및 소수점 비율 단위로 정규화
                                if 'market_kosdaq_roc' in sd['precomputed']:
                                    raw_k = float(sd['precomputed']['market_kosdaq_roc'][idx])
                                    locals_dict['market_kosdaq_roc'] = raw_k / 100.0 if abs(raw_k) > 0.5 else raw_k
                                
                                locals_dict['code'] = current_code
                                dt_obj = pd.to_datetime(row['datetime'])
                                locals_dict['datetime'] = dt_obj
                                locals_dict['feature_time'] = max(0, min(390, (dt_obj.hour * 60 + dt_obj.minute) - (9 * 60)))
                                locals_dict['current_price'] = current_price
                                
                                # --- 누락된 파생 지표 동기화 (strategy_utils.py 와 동일하게 구성) ---
                                roc_array = locals_dict.get('ROC', [])
                                locals_dict['ROC_recent'] = roc_array[-30:].tolist() if len(roc_array) > 0 else []
                                
                                high_array = locals_dict.get('high', [])
                                if len(high_array) > 1:
                                    recent_highs = high_array[-30:-1]
                                    if len(recent_highs) > 0:
                                        highest_recent = np.max(recent_highs)
                                        current_close = locals_dict.get('close', [0])[-1]
                                        locals_dict['is_pullback'] = bool(current_close < highest_recent)
                                    else:
                                        locals_dict['is_pullback'] = False
                                else:
                                    locals_dict['is_pullback'] = False
                                    
                                vol_array = locals_dict.get('volume', [])
                                if len(vol_array) > 0:
                                    locals_dict['avg_volume'] = float(np.mean(vol_array))
                                    locals_dict['volume_ratio'] = float(vol_array[-1]) / locals_dict['avg_volume'] if locals_dict['avg_volume'] > 0 else 1.0
                                    locals_dict['tick_avg_volume_10'] = float(np.mean(vol_array[-10:]))
                                    locals_dict['tick_avg_volume_5'] = float(np.mean(vol_array[-5:]))
                                    ma_20_vol = np.mean(vol_array[-20:])
                                    locals_dict['tick_volume_ma_ratio'] = float(vol_array[-1]) / ma_20_vol if ma_20_vol > 0 else 0.0
                                else:
                                    locals_dict['avg_volume'] = 0.0
                                    locals_dict['volume_ratio'] = 1.0
                                    locals_dict['tick_avg_volume_10'] = 0.0
                                    locals_dict['tick_avg_volume_5'] = 0.0
                                    locals_dict['tick_volume_ma_ratio'] = 0.0
                                    
                                locals_dict['ORDER_BOOK_IMBALANCE'] = np.array([0.0])
                                locals_dict['tick_order_book_imbalance'] = np.array([0.0])
                                
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
                                            debug_logs.append(f"❌ [{current_code}] 매수 평가 오류 ({stg_name_str}): {e}")
                                            
                                if not sd['first_eval_logged']:
                                    ai_str = f", AI_SCORE: {locals_dict.get('AI_SCORE', 0.0):.4f}" if uses_ai else ""
                                    debug_logs.append(f"🔍 [{current_code}] 첫 평가 - {current_time_str[5:16]}, 가격: {current_price:,.0f}{ai_str}")
                                    sd['first_eval_logged'] = True
                                    
                                if buy_signal:
                                    qty = int(invested_per_trade / current_price)
                                    if qty > 0:
                                        ai_str = f" | AI: {locals_dict.get('AI_SCORE', 0.0):.4f}" if uses_ai else ""
                                        debug_logs.append(f"[BUY_SIGNAL] 📈 [{current_code}] 매수 '{matched_stg_name}' 조건 충족!")
                                        matched_cond = next((code_str for name_str, code_str in buy_compiled if name_str == matched_stg_name), "")
                                        ind_lines = format_backtest_indicator_log(current_code, matched_stg_name, "매수", matched_cond, locals_dict)
                                        for line in ind_lines:
                                            debug_logs.append(line)
                                        
                                        debug_logs.append(f"📈 [{current_time_str[5:16]}] 매수 {current_code} | '{matched_stg_name}' | {current_price:,.0f}원 x {qty}주 = {current_price*qty:,.0f}원{ai_str} | 보유: {len(portfolio)+1}/{buycount}")
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
            if portfolio:
                debug_logs.append(f"─" * 50)
                debug_logs.append(f"⚠️ 잔여 포지션 {len(portfolio)}개 강제 청산 처리")
            for current_code, pos in list(portfolio.items()):
                # 마지막 가격은 precomputed 의 마지막 값을 참조
                sd = stock_data[current_code]
                last_price = float(sd['precomputed']['close'][-1])
                last_time = str(sd['precomputed']['datetime'][-1]).replace('T', ' ')
                
                sell_qty = pos['qty']
                trade_profit = (last_price - pos['buy_price']) * sell_qty
                trade_profit -= (pos['buy_price'] * sell_qty * 0.009)
                total_profit += trade_profit
                capital += trade_profit
                if trade_profit > 0: win_count += 1
                else: loss_count += 1
                real_profit_pct = (last_price - pos['buy_price']) / pos['buy_price'] * 100.0 - 0.9
                
                profit_emoji = '🟢' if trade_profit >= 0 else '🔴'
                debug_logs.append(f"{profit_emoji} [{current_code}] 강제청산 | {pos['buy_price']:,.0f}→{last_price:,.0f} ({real_profit_pct:+.2f}%) | 손익: {trade_profit:+,.0f}원")
                
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
            
            # --- 최종 결과 요약 로그 ---
            debug_logs.append("─" * 50)
            debug_logs.append(f"✅ [완료] 총 {total_trades}건 거래 | 승률: {win_rate:.1f}% ({win_count}승 {loss_count}패)")
            debug_logs.append(f"💰 총 손익: {total_profit:+,.0f}원 | 최종 자본: {capital:,.0f}원 | MDD: {mdd:.2f}%")
            if eval_errors > 0:
                debug_logs.append(f"⚠️ 전략 평가 오류 {eval_errors}건 발생 (상세는 위 로그 참조)")
            
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
                elif 'tick_close' in df_bnh.columns:
                    close_col = 'tick_close'
                    vol_col = 'tick_volume' if 'tick_volume' in df_bnh.columns else 'volume'
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
                "debug_logs": debug_logs[-5000:] # 프론트엔드 과부하 방지 (넉넉하게 상향)
            }
            
            if progress_callback: progress_callback(100, "시뮬레이션 완료!")
            return result
            
        except Exception as e:
            logger.error(f"백테스팅 오류: {e}", exc_info=True)
            return {"error": str(e), "debug_logs": [f"치명적 오류: {e}"]}

    def run_swing_backtest(self, start_date, end_date, code='ALL', progress_callback=None, initial_capital=10000000, buycount=3, target_profit=5.0, stop_loss=-3.0):
        """스윙 매매 전용 백테스팅 시뮬레이션 (일봉 15:28 종가 매수 & 다일 오버나잇 보유)"""
        try:
            from swing_strategy_utils import calc_daily_indicators, prepare_swing_locals, evaluate_swing_condition
            logger.info(f"📈 [스윙 백테스트] 시작: {start_date} ~ {end_date} (종목: {code})")
            if progress_callback: progress_callback(10, "스윙 백테스트용 일봉 데이터를 로딩 중입니다...")

            # 1. 스윙 전용 daily_candles 테이블 존재 확인 및 DDL 자동 생성
            s_dt = start_date.replace('-', '')
            e_dt = end_date.replace('-', '')
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS daily_candles (
                    code TEXT,
                    datetime TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume INTEGER,
                    amount REAL,
                    created_at TEXT,
                    PRIMARY KEY (code, datetime)
                )
            ''')
            conn.commit()
            
            q_daily = f"SELECT code, datetime, open, high, low, close, volume FROM daily_candles WHERE datetime >= '{s_dt}' AND datetime <= '{e_dt}'"
            if code and code != 'ALL':
                q_daily += f" AND code = '{code}'"
            q_daily += " ORDER BY datetime ASC"
            
            df_daily_all = pd.read_sql(q_daily, conn)
            conn.close()

            if not df_daily_all.empty:
                df_daily_all['date'] = df_daily_all['datetime'].apply(lambda x: f"{x[:4]}-{x[4:6]}-{x[6:8]}" if len(str(x))==8 else str(x))
                logger.info(f"✅ [스윙 백테스트] daily_candles 전용 테이블에서 {len(df_daily_all):,}건의 일봉 데이터 즉시 로드 성공!")
            else:
                # 2. daily_candles가 비어있는 경우 stock_data 기반 틱/분봉 데이터에서 일봉 합성 후 daily_candles에 저장
                logger.info("ℹ️ daily_candles 전용 테이블 데이터 부족으로 stock_data 기반 일봉 합성 생성 진행...")
                df = self.load_data(start_date, end_date, code)
                if df.empty:
                    if progress_callback: progress_callback(100, "해당 기간에 데이터가 없습니다.")
                    return {"error": "해당 기간에 데이터가 없습니다."}

                df['date'] = df['datetime'].str.slice(0, 10)
                
                c_col = 'tick_close' if 'tick_close' in df.columns else ('min3_close' if 'min3_close' in df.columns else 'close')
                o_col = 'tick_open' if 'tick_open' in df.columns else ('min3_open' if 'min3_open' in df.columns else 'open')
                h_col = 'tick_high' if 'tick_high' in df.columns else ('min3_high' if 'min3_high' in df.columns else 'high')
                l_col = 'tick_low' if 'tick_low' in df.columns else ('min3_low' if 'min3_low' in df.columns else 'low')
                v_col = 'tick_volume' if 'tick_volume' in df.columns else ('min3_volume' if 'min3_volume' in df.columns else 'volume')

                daily_rows = []
                for (c, d), group in df.groupby(['code', 'date']):
                    daily_rows.append({
                        'code': c,
                        'datetime': d.replace('-', ''),
                        'date': d,
                        'open': group[o_col].iloc[0] if o_col in group.columns else group[c_col].iloc[0],
                        'high': group[h_col].max() if h_col in group.columns else group[c_col].max(),
                        'low': group[l_col].min() if l_col in group.columns else group[c_col].min(),
                        'close': group[c_col].iloc[-1],
                        'volume': group[v_col].sum() if v_col in group.columns else len(group)
                    })

                df_daily_all = pd.DataFrame(daily_rows)
                df_daily_all.sort_values(by=['code', 'datetime'], inplace=True)

                # daily_candles 전용 테이블에 자동 영구 캐싱 저장
                try:
                    conn = sqlite3.connect(self.db_path)
                    cur = conn.cursor()
                    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    for _, r in df_daily_all.iterrows():
                        cur.execute('''
                            INSERT OR REPLACE INTO daily_candles
                            (code, datetime, open, high, low, close, volume, amount, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (r['code'], r['datetime'], r['open'], r['high'], r['low'], r['close'], r['volume'], 0, created_at))
                    conn.commit()
                    conn.close()
                    logger.info(f"💾 [스윙 DB] 합성된 일봉 {len(df_daily_all)}건을 daily_candles 전용 테이블에 저장 완료!")
                except Exception as save_err:
                    logger.error(f"❌ daily_candles 캐싱 실패: {save_err}")

            debug_logs = []
            debug_logs.append(f"📊 [스윙 시작] 백테스트 기간: {start_date} ~ {end_date}")
            debug_logs.append(f"💰 초기 자본금: {initial_capital:,.0f}원 | 스윙 최대 보유: {buycount}개 | 목표가: +{target_profit}% | 손절가: {stop_loss}%")
            debug_logs.append(f"📈 스윙 매수 조건: 15:28 종가 매수 (이격도 98~105, RSI < 70, 거래량 비율 >= 1.2)")

            capital = initial_capital
            max_capital = capital
            mdd = 0.0
            portfolio = {}
            trades = []
            win_count = 0
            loss_count = 0
            invest_per_trade = capital / max(1, buycount)

            trading_days = sorted(df_daily_all['date'].unique())
            total_days = len(trading_days)

            for idx, day_str in enumerate(trading_days):
                if progress_callback:
                    progress_callback(int(30 + (idx / max(1, total_days)) * 60), f"[{idx+1}/{total_days}] {day_str} 스윙 시뮬레이션 중...")

                day_df = df_daily_all[df_daily_all['date'] == day_str]

                # 1. 기존 보유 종목 매도 규칙 감시
                for c_code, holding in list(portfolio.items()):
                    stock_day = day_df[day_df['code'] == c_code]
                    if stock_day.empty:
                        holding['holding_days'] += 1
                        continue

                    row = stock_day.iloc[0]
                    c_price = row['close']
                    h_price = row['high']
                    l_price = row['low']

                    buy_p = holding['buy_price']
                    qty = holding['qty']

                    max_p_pct = ((h_price - buy_p) / buy_p * 100.0) - 0.3
                    min_p_pct = ((l_price - buy_p) / buy_p * 100.0) - 0.3

                    is_sold = False
                    sell_reason = ""
                    sell_p = c_price

                    if max_p_pct >= target_profit:
                        is_sold = True
                        sell_reason = f"스윙 목표가 달성 (+{target_profit}%)"
                        sell_p = buy_p * (1 + (target_profit + 0.3) / 100.0)
                    elif min_p_pct <= stop_loss:
                        is_sold = True
                        sell_reason = f"스윙 손절가 이탈 ({stop_loss}%)"
                        sell_p = buy_p * (1 + (stop_loss + 0.3) / 100.0)
                    elif holding['holding_days'] >= 5:
                        is_sold = True
                        sell_reason = "보유 기간(5일) 만료 타임컷"
                        sell_p = c_price

                    if is_sold:
                        trade_profit = (sell_p - buy_p) * qty - (buy_p * qty * 0.003)
                        capital += trade_profit
                        if trade_profit >= 0: win_count += 1
                        else: loss_count += 1

                        real_profit_pct = (sell_p - buy_p) / buy_p * 100.0 - 0.3
                        debug_logs.append(f"🟢 [스윙 매도 {day_str}] {c_code} | {sell_reason} | {buy_p:,.0f}→{sell_p:,.0f} ({real_profit_pct:+.2f}%) | 손익: {trade_profit:+,.0f}원")
                        trades.append({
                            'code': c_code,
                            'buy_time': holding['buy_date'],
                            'sell_time': day_str,
                            'buy_price': buy_p,
                            'sell_price': sell_p,
                            'qty': qty,
                            'profit_pct': real_profit_pct,
                            'profit_amount': trade_profit,
                            'sell_stg': sell_reason
                        })
                        del portfolio[c_code]
                        max_capital = max(max_capital, capital)
                        mdd = max(mdd, (max_capital - capital) / max_capital * 100.0)
                    else:
                        holding['holding_days'] += 1

                # 2. 15:28 신규 스윙 매수 종목 평가 및 종가 체결
                available_slots = buycount - len(portfolio)
                if available_slots > 0:
                    candidates = []
                    for _, row in day_df.iterrows():
                        c_code = row['code']
                        if c_code in portfolio:
                            continue

                        hist_df = df_daily_all[(df_daily_all['code'] == c_code) & (df_daily_all['date'] <= day_str)]
                        if len(hist_df) < 5:
                            continue

                        safe_locals = prepare_swing_locals(c_code, hist_df)
                        if not safe_locals:
                            continue

                        rule = "98.0 <= disparity20 <= 105.0 and rsi14 < 70.0 and volume_ratio >= 1.2"
                        if evaluate_swing_condition(rule, safe_locals):
                            candidates.append({
                                'code': c_code,
                                'price': row['close'],
                                'score': safe_locals.get('volume_ratio', 1.0) * safe_locals.get('disparity20', 100.0)
                            })

                    candidates.sort(key=lambda x: x['score'], reverse=True)
                    for cand in candidates[:available_slots]:
                        c_code = cand['code']
                        buy_p = cand['price']
                        if buy_p <= 0: continue
                        qty = max(1, int(invest_per_trade / buy_p))

                        portfolio[c_code] = {
                            'buy_price': buy_p,
                            'qty': qty,
                            'buy_date': day_str,
                            'holding_days': 1
                        }
                        debug_logs.append(f"📈 [스윙 15:28 매수 {day_str}] {c_code} | {buy_p:,.0f}원 x {qty}주 = {buy_p * qty:,.0f}원 체결")

            total_trades = win_count + loss_count
            win_rate = (win_count / total_trades * 100.0) if total_trades > 0 else 0.0
            total_profit = capital - initial_capital

            debug_logs.append("─" * 50)
            debug_logs.append(f"✅ [스윙 완료] 총 {total_trades}건 거래 | 승률: {win_rate:.1f}% ({win_count}승 {loss_count}패)")
            debug_logs.append(f"💰 총 손익: {total_profit:+,.0f}원 | 최종 자본: {capital:,.0f}원 | MDD: {mdd:.2f}%")

            return {
                "total_trades": total_trades,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate": round(win_rate, 2),
                "total_profit": round(total_profit, 0),
                "final_capital": round(capital, 0),
                "mdd": round(mdd, 2),
                "trades": trades,
                "history": [],
                "bnh_history": [],
                "uses_ai": False,
                "debug_logs": debug_logs[-5000:]
            }

        except Exception as e:
            logger.error(f"스윙 백테스팅 오류: {e}", exc_info=True)
            return {"error": str(e), "debug_logs": [f"스윙 백테스트 오류: {e}"]}

if __name__ == '__main__':
    bt = Backtester()
    def p(prog, msg): print(f"[{prog}%] {msg}")
    res = bt.run('2026-06-01', '2026-06-13', progress_callback=p)
    print("\n[백테스트 결과]")
    for k, v in res.items():
        if k != 'trades':
            print(f"{k}: {v}")
