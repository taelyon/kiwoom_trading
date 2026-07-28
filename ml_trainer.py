import logging
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime
import os
import sys
import threading

# LightGBM 라이브러리 임포트 시도
try:
    import lightgbm as lgb
    LGBM_AVAILABLE = True
except ImportError:
    LGBM_AVAILABLE = False

from utils import CallbackSignal

class MLTrainingWorker(threading.Thread):
    """
    백그라운드에서 머신러닝 모델을 학습하는 워커 스레드
    UI 프리징(멈춤)을 방지하기 위해 threading.Thread 상속
    """

    def __init__(self, db_path='data/stock_data.db', model_output_path='lgbm_model.txt', on_progress=None, on_finished=None,
                 start_date=None, end_date=None, hyperparameters=None, save_history=True):
        super().__init__()
        self.db_path = db_path
        self.model_output_path = model_output_path
        self.start_date = start_date
        self.end_date = end_date
        self.save_history = save_history
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # 콜백 기반 시그널 초기화
        self.finished_signal = CallbackSignal()
        if on_finished:
            self.finished_signal.connect(on_finished)
        self.progress_signal = CallbackSignal()
        if on_progress:
            self.progress_signal.connect(on_progress)
        
        # 학습 파라미터 기본값
        self.params = {
            'objective': 'binary',
            'metric': 'auc',
            'boosting_type': 'gbdt',
            'num_leaves': 32,
            'max_depth': 6,
            'learning_rate': 0.02,
            'feature_fraction': 0.7,
            'bagging_fraction': 0.7,
            'bagging_freq': 5,
            'verbose': -1,
            'min_data_in_leaf': 50,
            'lambda_l1': 0.1,
            'lambda_l2': 1.0,
            'is_unbalance': True,
            'force_col_wise': True
        }
        if hyperparameters:
            self.params.update(hyperparameters)

    def run(self):
        """스레드 실행 메인 함수"""
        if not LGBM_AVAILABLE:
            error_msg = "LightGBM이 설치되지 않았습니다. 터미널에서 'pip install lightgbm'을 실행하세요."
            self.logger.error(error_msg)
            self.finished_signal.emit(False, error_msg, None)
            return

        conn = None
        try:
            self.progress_signal.emit("🔍 [ML] 학습 데이터 로딩 중 (DB)...")
            
            if not os.path.exists(self.db_path):
                self.finished_signal.emit(False, f"데이터베이스 파일이 없습니다: {self.db_path}", None)
                return

            conn = sqlite3.connect(self.db_path)
            
            # 학습에 필요한 핵심 컬럼만 조회 (데이터 양이 많을 수 있으므로 필요한 것만)
            query = """
                SELECT *
                FROM stock_data 
                WHERE tick_velocity IS NOT NULL 
                  AND tick_velocity != 0
            """
            params = []
            if self.start_date:
                query += " AND datetime >= ?"
                params.append(f"{self.start_date} 00:00:00")
            if self.end_date:
                query += " AND datetime <= ?"
                params.append(f"{self.end_date} 23:59:59")
            query += " ORDER BY code, datetime"
            
            df = pd.read_sql(query, conn, params=params)
            
            if df.empty:
                self.finished_signal.emit(False, "[ML] 학습할 데이터가 부족하여 학습을 건너뜁니다.", None)
                return

            self.progress_signal.emit(f"🔍 [ML] 데이터 전처리 중... (Rows: {len(df)})")

            # === KOSDAQ 지수 데이터 병합 ===
            try:
                kosdaq_query = "SELECT datetime as kosdaq_time, close as kosdaq_close, open as kosdaq_open FROM kosdaq_3m"
                kosdaq_df = pd.read_sql(kosdaq_query, conn)
                
                if not kosdaq_df.empty:
                    # datetime 형식이 YYYYMMDDHHMMSS 와 YYYY-MM-DD HH:MM:SS 혼용될 수 있으므로 format 파라미터 제거
                    df['dt_obj'] = pd.to_datetime(df['datetime'], errors='coerce')
                    kosdaq_df['dt_obj'] = pd.to_datetime(kosdaq_df['kosdaq_time'], errors='coerce')
                    
                    kosdaq_df = kosdaq_df.sort_values('dt_obj').dropna(subset=['dt_obj'])
                    
                    # 당일 KOSDAQ 시가 계산
                    kosdaq_df['date'] = kosdaq_df['dt_obj'].dt.date
                    daily_open = kosdaq_df.groupby('date')['kosdaq_open'].transform('first')
                    
                    # KOSDAQ 등락률 (현재지수 vs 당일시가)
                    kosdaq_df['market_kosdaq_roc'] = np.where(daily_open > 0, 
                                                            (kosdaq_df['kosdaq_close'] - daily_open) / daily_open, 
                                                            0.0)
                    
                    # 매수 시점(틱) 기준 가장 최근의 KOSDAQ 분봉 데이터 매칭
                    df = df.sort_values('dt_obj').dropna(subset=['dt_obj'])
                    df = pd.merge_asof(df, kosdaq_df[['dt_obj', 'market_kosdaq_roc']], on='dt_obj', direction='backward')
                    
                    # 기존 로직을 위해 원래 정렬 순서로 복구
                    df = df.sort_values(['code', 'datetime'])
                else:
                    df['market_kosdaq_roc'] = 0.0
            except Exception as e:
                self.logger.error(f"KOSDAQ 병합 중 오류: {e}")
                df['market_kosdaq_roc'] = 0.0

            # === Feature Engineering (비율/속도 변환) ===
            # 종목별로 그룹화하여 계산 필요
            
            # 1. Target 생성 (Path Dependence 고려)
            # 기존: 종가만 비교 → 캔들 내에서 발생한 익절/손절 변동성 무시
            # 개선: 미래 30틱 내 고가(high)가 익절선(+1.0%)에 먼저 닿는지, 저가(low)가 손절선(-1.2%)에 먼저 닿는지 확인
            LOOKAHEAD = 30  # 향후 30틱 (약 5~15분 초스캘핑)
            TARGET_PCT = 0.010  # +1.0% (30틱 내 빠른 1차 익절 도달 기준)
            STOP_PCT = -0.012   # -1.2% (슬림화된 손절 방어선)
            
            # 향후 1~30틱 고가/저가 매트릭스 생성
            future_high_shifts = [df.groupby('code')['tick_high'].shift(-i) for i in range(1, LOOKAHEAD + 1)]
            future_low_shifts = [df.groupby('code')['tick_low'].shift(-i) for i in range(1, LOOKAHEAD + 1)]
            
            future_high_prices = pd.concat(future_high_shifts, axis=1)
            future_low_prices = pd.concat(future_low_shifts, axis=1)
            
            # 30틱이 안 되는 가장 최근 데이터들은 드롭 (미래를 알 수 없으므로 학습 제외)
            df = df.dropna(subset=future_high_prices.columns, how='any').copy()
            future_high_prices = future_high_prices.loc[df.index]
            future_low_prices = future_low_prices.loc[df.index]
            
            # 변화율 매트릭스 (기준은 매수 시점의 현재 캔들 종가)
            future_high_returns = (future_high_prices.div(df['tick_close'], axis=0)) - 1.0
            future_low_returns = (future_low_prices.div(df['tick_close'], axis=0)) - 1.0
            
            # 조건 도달 확인
            target_hits = (future_high_returns >= TARGET_PCT)
            stop_hits = (future_low_returns <= STOP_PCT)
            
            # 도달한 첫 번째 시점(컬럼 인덱스) 구하기. 도달하지 않으면 무한대(inf) 부여
            target_idx = np.where(target_hits.any(axis=1), target_hits.values.argmax(axis=1), float('inf'))
            stop_idx = np.where(stop_hits.any(axis=1), stop_hits.values.argmax(axis=1), float('inf'))
            
            # 손절보다 익절에 먼저 도달한 경우만 1 
            # (같은 캔들에서 둘 다 터치한 경우 target_idx == stop_idx 이며, 이는 False가 되어 보수적으로 0점(손절) 처리됨)
            df['label'] = (target_idx < stop_idx).astype(int)
            
            # 2. 비율 변환 (가격 자체는 제거)
            # 이미 RELATIVE_POSITION(이격도), STRENGTH(체결강도) 등은 비율임.
            # 추가적으로 필요한 것들 변환
            
            # [B] tick_velocity 로그 정규화 (극단적 스케일 0~999999 해소)
            df['tick_velocity'] = np.log1p(df['tick_velocity'])
            
            # 거래량 이동평균 비율 (Volume MA Ratio)
            # 현재 거래량 / 20봉 이동평균 거래량
            df['vol_ma20'] = df.groupby('code')['tick_volume'].transform(lambda x: x.rolling(20, min_periods=1).mean())
            df['tick_volume_ma_ratio'] = np.where(df['vol_ma20'] > 0, df['tick_volume'] / df['vol_ma20'], 0)
            
            # (삭제됨) 거래 대금 가속도는 거래량 폭발력과 중복도가 높아 제거

            import talib
            def calc_new_indicators(g):
                close = g['tick_close'].values
                high = g['tick_high'].values
                low = g['tick_low'].values
                vol = g['tick_volume'].values
                
                VWAP_WINDOW = 60
                typ = (high + low + close) / 3
                rolling_pv = pd.Series(typ * vol).rolling(VWAP_WINDOW, min_periods=1).sum().values
                rolling_v = pd.Series(vol).rolling(VWAP_WINDOW, min_periods=1).sum().values
                vwap = np.where(rolling_v > 0, rolling_pv / rolling_v, close)
                tick_vwap_distance = np.where(vwap > 0, (close - vwap) / vwap, 0)
                
                if len(close) >= 26:
                    _, _, h = talib.MACD(close)
                    tick_macd_hist = pd.Series(h).fillna(0.0).values
                else:
                    tick_macd_hist = np.zeros(len(close))
                    
                if len(close) >= 21:
                    tick_rsi21 = pd.Series(talib.RSI(close, timeperiod=21)).fillna(50.0).values
                else:
                    tick_rsi21 = np.full(len(close), 50.0)
                    
                tick_price_roc = g['tick_close'].pct_change(periods=10).fillna(0.0).values
                
                vol_sum_5 = g['tick_volume'].rolling(5).sum()
                prev_vol_sum_5 = vol_sum_5.shift(5)
                tick_vol_roc = pd.Series(np.where(prev_vol_sum_5 > 0, vol_sum_5 / prev_vol_sum_5, 1.0)).fillna(1.0).values
                    
                return pd.DataFrame({
                    'tick_vwap_distance': tick_vwap_distance,
                    'tick_macd_hist': tick_macd_hist,
                    'tick_rsi21': tick_rsi21,
                    'tick_price_roc': tick_price_roc,
                    'tick_vol_roc': tick_vol_roc
                }, index=g.index)
            
            # [DB 우선 사용 메커니즘] DB에 해당 지표 컬럼이 존재하고 유효 수치(notnull)가 존재하면 DB 수치를 직접 사용하고, 없거나 NULL인 경우만 즉석 연산
            new_cols = df.groupby('code', group_keys=False).apply(calc_new_indicators)
            for col in new_cols.columns:
                if col not in df.columns or df[col].notnull().sum() == 0:
                    df[col] = new_cols[col]
                else:
                    df[col] = df[col].fillna(new_cols[col])
            
            # [DB 우선] 돌파 가속도 (Impulse = log_velocity * price_roc)
            if 'tick_impulse' not in df.columns or df['tick_impulse'].notnull().sum() == 0:
                df['tick_impulse'] = (df['tick_velocity'] * df['tick_price_roc']).fillna(0.0)
            else:
                df['tick_impulse'] = df['tick_impulse'].fillna(0.0)
            
            # [DB 우선] ATR% (상대적 ATR 변동성 비율 %)
            if 'tick_atr_ratio' not in df.columns or df['tick_atr_ratio'].notnull().sum() == 0:
                tr1 = df['tick_high'] - df['tick_low']
                prev_close = df.groupby('code')['tick_close'].shift(1).fillna(df['tick_open'])
                tr2 = (df['tick_high'] - prev_close).abs()
                tr3 = (df['tick_low'] - prev_close).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                df['tr'] = tr
                df['atr20'] = df.groupby('code')['tr'].transform(lambda x: x.rolling(20, min_periods=1).mean())
                df['tick_atr_ratio'] = np.where(df['tick_close'] > 0, (df['atr20'] / df['tick_close']) * 100.0, 0.0)
                df['tick_atr_ratio'] = df['tick_atr_ratio'].fillna(0.0)
            else:
                df['tick_atr_ratio'] = df['tick_atr_ratio'].fillna(0.0)

            # [DB 우선] 이동평균선 정배열 척도 (MA Ribbon Distance)
            if 'tick_ma_spread' not in df.columns or df['tick_ma_spread'].notnull().sum() == 0:
                df['tick_ma_spread'] = np.where(df['tick_ma20'] > 0, (df['tick_ma5'] - df['tick_ma20']) / df['tick_ma20'], 0)
            else:
                df['tick_ma_spread'] = df['tick_ma_spread'].fillna(0.0)
            
            # [DB 우선] 캔들 윗꼬리 비율 (tail_ratio)
            if 'tick_tail_ratio' not in df.columns or df['tick_tail_ratio'].notnull().sum() == 0:
                df['tick_tail_ratio'] = np.where((df['tick_high'] - df['tick_low']) > 0, (df['tick_high'] - df['tick_close']) / (df['tick_high'] - df['tick_low']), 0)
            else:
                df['tick_tail_ratio'] = df['tick_tail_ratio'].fillna(0.0)
            
            # [DB 우선] 봉 내 가격 변동폭 비율 (tick_spread)
            if 'tick_spread' not in df.columns or df['tick_spread'].notnull().sum() == 0:
                df['tick_spread'] = np.where(df['tick_close'] > 0, (df['tick_high'] - df['tick_low']) / df['tick_close'], 0)
            else:
                df['tick_spread'] = df['tick_spread'].fillna(0.0)
            
            # [DB 우선] 틱 이격도 (tick_disparity20)
            if 'tick_disparity20' not in df.columns or df['tick_disparity20'].notnull().sum() == 0:
                df['tick_disparity20'] = np.where(df['tick_ma20'] > 0, (df['tick_close'] / df['tick_ma20']) * 100, 100.0)
            else:
                df['tick_disparity20'] = df['tick_disparity20'].fillna(100.0)
            
            # [DB 우선] 볼린저 밴드 포지션 (tick_bb_position)
            if 'tick_bb_position' not in df.columns or df['tick_bb_position'].notnull().sum() == 0:
                df['std20'] = df.groupby('code')['tick_close'].transform(lambda x: x.rolling(20, min_periods=1).std(ddof=1).fillna(0))
                bb_upper = df['tick_ma20'] + (2 * df['std20'])
                bb_lower = df['tick_ma20'] - (2 * df['std20'])
                df['tick_bb_position'] = np.where((bb_upper - bb_lower) > 0, (df['tick_close'] - bb_lower) / (bb_upper - bb_lower), 0.5)
                df['tick_bb_position'] = df['tick_bb_position'].fillna(0.5)
            else:
                df['tick_bb_position'] = df['tick_bb_position'].fillna(0.5)
            
            # [DB 우선] 호가 잔량 비율 (tick_imbalance)
            if 'tick_imbalance' in df.columns and df['tick_imbalance'].notnull().sum() > 0:
                df['tick_imbalance'] = df['tick_imbalance'].fillna(0.5)
            else:
                df['tick_imbalance'] = 0.5
            
            # Feature 목록 정의 (노이즈 피처 제거: tick_volume_ma_ratio, tick_vol_roc 제거)
            base_features = [
                'tick_strength', 
                'tick_velocity', 
                'min3_relative_position'
            ]
            
            new_features = [
                'tick_vwap_distance',
                'tick_macd_hist',
                'tick_rsi21',
                'tick_price_roc',      # 가격 상승 가속도
                'tick_impulse',        # [신규 추가] 돌파 가속도 (tick_velocity * tick_price_roc)
                'tick_atr_ratio',      # [신규 추가] 상대적 ATR 변동성 비율 (%)
                'tick_ma_spread',      # [추가] 이평선 정배열 척도
                'tick_tail_ratio',     # [추가] 캔들 윗꼬리 비율
                'tick_spread',         # [추가] 봉 내 가격 변동폭 비율
                'tick_disparity20',    # [추가] 20틱 이평 이격도
                'tick_bb_position',    # [추가] 볼린저밴드 상단/하단 위치
                'tick_imbalance',      # [추가] 실시간 호가 잔량 비율
                'market_kosdaq_roc'    # [추가] 당일 코스닥 등락률
            ]
            
            features = base_features + new_features
            
            # 신규 피처 호환성 처리 (과거 데이터 결측치 0으로 채우기)
            for nf in new_features:
                if nf not in df.columns:
                    df[nf] = 0.0
                else:
                    df[nf] = df[nf].fillna(0.0)
            
            # 결측치 제거
            df_train = df.dropna(subset=base_features + ['label'])
            
            if len(df_train) < 50:
                self.finished_signal.emit(False, f"[ML] 유효한 학습 데이터가 너무 적습니다 ({len(df_train)}개). 최소 50개 필요.", None)
                return
            
            X = df_train[features]
            y = df_train['label']
            
            # 양성 라벨 비율 로깅 (데이터 밸런스 확인)
            pos_ratio = y.mean()
            self.progress_signal.emit(f"📊 [ML] 양성 라벨 비율: {pos_ratio:.1%} (1={y.sum()}, 0={len(y)-y.sum()})")
            
            # 학습용/검증용 분리 (75:25, 시계열 누수 방지를 위한 갭 포함)
            # train[0..74%] → gap(1%) → val[75%..100%]
            # 갭: 라벨 생성 시 미래 데이터(LOOKAHEAD 틱)를 참조하므로, train 마지막과 val 첫 부분이 겹치는 것을 방지
            gap_size = min(LOOKAHEAD * 2, int(len(X) * 0.01))  # 최소 LOOKAHEAD*2, 최대 1%
            split_idx = int(len(X) * 0.75)
            X_train, X_val = X.iloc[:split_idx], X.iloc[split_idx + gap_size:]
            y_train, y_val = y.iloc[:split_idx], y.iloc[split_idx + gap_size:]
            
            # 동적 파라미터 조정 (데이터가 적을 때 LightGBM 에러 방지)
            train_len = len(X_train)
            if 'min_data_in_leaf' in self.params:
                if train_len < self.params['min_data_in_leaf'] * 2:
                    new_min_data = max(1, int(train_len / 4))
                    self.logger.warning(f"학습 데이터({train_len}개)가 적어 min_data_in_leaf를 {self.params['min_data_in_leaf']}에서 {new_min_data}(으)로 임시 조정합니다.")
                    self.params['min_data_in_leaf'] = new_min_data
                    
            if 'num_leaves' in self.params:
                max_leaves = max(2, int(train_len / 5))
                if self.params['num_leaves'] > max_leaves:
                    self.logger.warning(f"학습 데이터({train_len}개)가 적어 num_leaves를 {self.params['num_leaves']}에서 {max_leaves}(으)로 임시 조정합니다.")
                    self.params['num_leaves'] = max_leaves
            
            # LightGBM 데이터셋 생성
            train_data = lgb.Dataset(X_train, label=y_train)
            val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)
            
            self.progress_signal.emit("🤖 [ML] LightGBM 모델 학습 시작...")
            
            # 모델 학습
            model = lgb.train(
                self.params,
                train_data,
                num_boost_round=2000,        # 반복 횟수 (Early Stopping 의존)
                valid_sets=[train_data, val_data],
                callbacks=[
                    lgb.early_stopping(stopping_rounds=100), # 과적합 방지: 100번 동안 성능 향상 없으면 중단
                    lgb.log_evaluation(period=100) 
                ]
            )
            
            self.progress_signal.emit("💾 [ML] 모델 히스토리 파일 저장 중...")
            # 검증 성능(AUC) 가져오기
            best_score = model.best_score.get('valid_1', {}).get('auc', 0.0) if hasattr(model, 'best_score') else 0.0
            best_train_score = model.best_score.get('training', {}).get('auc', 0.0) if hasattr(model, 'best_score') else 0.0
            
            # Feature Importance 로깅 (split 방식 대신 gain 방식으로 변경하여 실질적 기여도 측정)
            raw_importance = model.feature_importance(importance_type='gain')
            importance = list(zip(features, raw_importance))
            importance.sort(key=lambda x: x[1], reverse=True)
            top_features = ", ".join([f"{f}:{int(score)}" for f, score in importance])
            
            import json
            metrics = {
                'auc': best_score,
                'train_auc': best_train_score,
                'data_rows': len(df_train),
                'feature_importance': [{'feature': f, 'importance': int(s)} for f, s in importance]
            }
            
            if self.save_history:
                try:
                    os.makedirs('models', exist_ok=True)
                    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                    hist_path = f"models/lgbm_model_{ts}.txt"
                    model.save_model(hist_path)
                    
                    meta_path = f"models/lgbm_model_{ts}.json"
                    meta = {
                        'timestamp': ts,
                        'params': self.params,
                        'start_date': self.start_date,
                        'end_date': self.end_date,
                        'metrics': metrics
                    }
                    with open(meta_path, 'w', encoding='utf-8') as mf:
                        json.dump(meta, mf, ensure_ascii=False, indent=2)
                except Exception as e:
                    self.logger.error(f"히스토리 모델 저장 실패: {e}")
                    
                # ------------------- 런타임 자동 배포 로직 시작 -------------------
                current_deployed_auc = 0.0
                try:
                    if os.path.exists('data/lgbm_model_params.json'):
                        import json
                        with open('data/lgbm_model_params.json', 'r', encoding='utf-8') as f:
                            old_meta = json.load(f)
                            current_deployed_auc = old_meta.get('metrics', {}).get('auc', 0.0)
                except Exception:
                    pass
                    
                self.progress_signal.emit(f"⚖️ [비교] 현재 배포 모델 AUC: {current_deployed_auc:.4f} vs 신규 단일 모델 AUC: {best_score:.4f}")
                
                if best_score > current_deployed_auc and best_score > 0:
                    self.progress_signal.emit(f"🎉 신규 모델 성능 향상 확인! 실시간 자동 배포를 진행합니다.")
                    try:
                        import shutil
                        if os.path.exists(hist_path):
                            shutil.copy2(hist_path, 'lgbm_model.txt')
                            if os.path.exists(meta_path):
                                shutil.copy2(meta_path, 'lgbm_model.json')
                                os.makedirs('data', exist_ok=True)
                                shutil.copy2(meta_path, 'data/lgbm_model_params.json')
                            
                            try:
                                import strategy_utils
                                strategy_utils.reload_model()
                            except:
                                pass
                            
                            self.finished_signal.emit(True, f"성공적으로 단일 학습 모델 자동 배포 완료 (AUC: {best_score:.4f})", metrics)
                            return
                    except Exception as e:
                        self.logger.error(f"자동 배포 중 오류: {e}")
                else:
                    self.progress_signal.emit(f"🛡 신규 모델의 성능이 기존 모델보다 우수하지 않아 배포를 취소하고 백업만 유지합니다.")
                # ------------------- 런타임 자동 배포 로직 끝 -------------------
            
            success_msg = f"✅ 모델 학습 완료! (Data: {len(df_train)}, 검증 AUC: {best_score:.4f}, Top: {top_features})"
            self.logger.debug(success_msg)
            self.finished_signal.emit(True, success_msg, metrics)
        except Exception as ex:
            self.logger.error(f"모델 학습 중 치명적 오류: {ex}", exc_info=True)
            self.finished_signal.emit(False, f"학습 중 오류 발생: {ex}", None)
            
        finally:
            if conn:
                conn.close()

class MLGridSearchWorker(threading.Thread):
    def __init__(self, db_path='data/stock_data.db', model_output_path='lgbm_model.txt', on_progress=None, on_finished=None,
                 start_date=None, end_date=None, save_history=True):
        super().__init__()
        self.db_path = db_path
        self.model_output_path = model_output_path
        self.start_date = start_date
        self.end_date = end_date
        self.logger = logging.getLogger(self.__class__.__name__)
        
        self.progress_signal = CallbackSignal()
        self.finished_signal = CallbackSignal()
        
        if on_progress:
            self.progress_signal.connect(on_progress)
        if on_finished:
            self.finished_signal.connect(on_finished)
            
        self.param_grid = [
            {"max_depth": 3, "num_leaves": 8, "min_data_in_leaf": 300, "learning_rate": 0.02}, # 극단적 안정형
            {"max_depth": 4, "num_leaves": 16, "min_data_in_leaf": 200, "learning_rate": 0.02}, # 안정형
            {"max_depth": 5, "num_leaves": 24, "min_data_in_leaf": 150, "learning_rate": 0.02}, # 약한 균형형
            {"max_depth": 5, "num_leaves": 32, "min_data_in_leaf": 100, "learning_rate": 0.02}, # 균형형 (사용자 제안)
            {"max_depth": 5, "num_leaves": 32, "min_data_in_leaf": 100, "learning_rate": 0.05}, # 균형형 (빠른 학습)
            {"max_depth": 6, "num_leaves": 32, "min_data_in_leaf": 50, "learning_rate": 0.01},  # 공격형 (정밀 학습)
            {"max_depth": 6, "num_leaves": 32, "min_data_in_leaf": 50, "learning_rate": 0.02},  # 공격형 (기본값)
            {"max_depth": 7, "num_leaves": 64, "min_data_in_leaf": 30, "learning_rate": 0.02},  # 초공격형
        ]

    def run(self):
        self.progress_signal.emit(f"🚀 [Grid Search] 총 {len(self.param_grid)}개의 하이퍼파라미터 조합 최적화 학습을 시작합니다.")
        
        best_auc = 0.0
        best_params = None
        best_ts = None
        
        for i, params in enumerate(self.param_grid):
            self.progress_signal.emit(f"⏳ [Grid Search] ({i+1}/{len(self.param_grid)}) 모델 학습 중... 파라미터: {params}")
            
            current_success = False
            current_metrics = None
            
            def local_on_finished(success, msg, metrics):
                nonlocal current_success, current_metrics
                current_success = success
                current_metrics = metrics
            
            worker = MLTrainingWorker(
                db_path=self.db_path,
                model_output_path=self.model_output_path,
                on_progress=lambda msg: self.progress_signal.emit(f"   {msg}"),
                on_finished=local_on_finished,
                start_date=self.start_date,
                end_date=self.end_date,
                hyperparameters=params,
                save_history=True
            )
            
            worker.run()
            
            if current_success and current_metrics:
                auc = current_metrics.get('auc', 0.0)
                if auc > best_auc:
                    best_auc = auc
                    best_params = params
                    try:
                        models_dir = 'models'
                        if os.path.exists(models_dir):
                            files = [f for f in os.listdir(models_dir) if f.endswith('.json')]
                            files.sort(key=lambda x: os.path.getmtime(os.path.join(models_dir, x)), reverse=True)
                            if files:
                                best_ts = files[0].replace('lgbm_model_', '').replace('.json', '')
                    except Exception as e:
                        self.logger.error(f"최근 생성 모델 찾기 실패: {e}")
        
        if not best_ts:
            self.finished_signal.emit(False, "[Grid Search] 유효한 모델을 찾지 못했습니다.", None)
            return

        self.progress_signal.emit(f"🏁 [Grid Search 완료] 최고 성능 검증 AUC: {best_auc:.4f} (파라미터: {best_params})")
        
        current_deployed_auc = 0.0
        try:
            if os.path.exists('data/lgbm_model_params.json'):
                import json
                with open('data/lgbm_model_params.json', 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                    current_deployed_auc = meta.get('metrics', {}).get('auc', 0.0)
        except Exception:
            pass
            
        self.progress_signal.emit(f"⚖️ [비교] 현재 배포 모델 AUC: {current_deployed_auc:.4f} vs 신규 최적 모델 AUC: {best_auc:.4f}")
        
        if best_auc > current_deployed_auc and best_auc > 0:
            self.progress_signal.emit(f"🎉 신규 최적 모델 성능 향상 확인! 실시간 자동 배포를 진행합니다.")
            try:
                import shutil
                src_model = f"models/lgbm_model_{best_ts}.txt"
                if os.path.exists(src_model):
                    shutil.copy2(src_model, 'lgbm_model.txt')
                    src_json = f"models/lgbm_model_{best_ts}.json"
                    if os.path.exists(src_json):
                        shutil.copy2(src_json, 'lgbm_model.json')
                        os.makedirs('data', exist_ok=True)
                        shutil.copy2(src_json, 'data/lgbm_model_params.json')
                    
                    try:
                        import strategy_utils
                        strategy_utils.reload_model()
                    except:
                        pass
                    
                    self.finished_signal.emit(True, f"성공적으로 최적 모델 자동 배포 완료 (AUC: {best_auc:.4f})", None)
                    return
            except Exception as e:
                self.logger.error(f"자동 배포 중 오류: {e}")
                self.finished_signal.emit(False, f"자동 배포 실패: {e}", None)
        else:
            self.finished_signal.emit(True, f"기존 배포 모델의 성능이 더 우수하여 교체하지 않습니다.", None)

if __name__ == "__main__":
    # 단독 실행 모드 (자동 학습 그리드 서치 및 조건부 배포)
    import json
    import asyncio
    
    print("🚀 [Standalone] ML 학습 프로세스 시작 (Hyperparameter Search)")
    
    # 1. 시도할 파라미터 셋 (안정형, 균형형, 공격형)
    param_grid = [
        {"max_depth": 4, "num_leaves": 16, "min_data_in_leaf": 200}, # 안정형 (과적합 방지 최우선)
        {"max_depth": 5, "num_leaves": 24, "min_data_in_leaf": 100}, # 균형형
        {"max_depth": 6, "num_leaves": 32, "min_data_in_leaf": 50},  # 공격형 (복잡한 패턴 학습)
    ]
    
    best_auc = 0.0
    best_ts = None
    best_params = None
    
    # 2. 순차적으로 학습 진행
    for params in param_grid:
        print(f"\n▶️ [학습 시작] 하이퍼파라미터: {params}")
        
        current_metrics = {}
        current_success = False
        current_ts = None
        
        def on_progress(msg):
            # 로그 과다 방지
            if "[ML]" in msg:
                print("   " + msg)
                
        def on_finished(success, msg, metrics=None):
            global current_success, current_metrics
            current_success = success
            if metrics:
                current_metrics = metrics
            print(f"   결과: {'✅ 성공' if success else '❌ 실패'}, {msg}")
            
        worker = MLTrainingWorker(on_progress=on_progress, on_finished=on_finished, hyperparameters=params)
        worker.run()
        
        if current_success and current_metrics:
            try:
                models_dir = 'models'
                if os.path.exists(models_dir):
                    json_files = [f for f in os.listdir(models_dir) if f.startswith('lgbm_model_') and f.endswith('.json')]
                    if json_files:
                        json_files.sort(reverse=True)
                        latest_json = json_files[0]
                        current_ts = latest_json.replace('lgbm_model_', '').replace('.json', '')
            except Exception as e:
                print(f"   ⚠️ TS 파싱 에러: {e}")
            
            auc = current_metrics.get('auc', 0.0)
            if auc > best_auc:
                best_auc = auc
                best_ts = current_ts
                best_params = params
                
    print(f"\n🏆 [학습 완료] 최고 성능(AUC): {best_auc:.4f} (파라미터: {best_params}, 버젼: {best_ts})")
    
    # 3. 기존 모델과 성능 비교
    current_deployed_auc = 0.0
    try:
        if os.path.exists('data/lgbm_model_params.json'):
            with open('data/lgbm_model_params.json', 'r', encoding='utf-8') as f:
                meta = json.load(f)
                current_deployed_auc = meta.get('metrics', {}).get('auc', 0.0)
    except Exception as e:
        print(f"⚠️ 기존 모델 메타데이터 읽기 실패: {e}")
        
    print(f"📊 [비교] 현재 적용된 모델 AUC: {current_deployed_auc:.4f} vs 새로 학습된 최고 모델 AUC: {best_auc:.4f}")
    
    # 4. 자동 핫-리로드(배포)
    if best_auc > current_deployed_auc and best_ts:
        print(f"✨ 새 모델의 성능이 더 우수하여 실시간 매매 봇에 자동 배포(Hot-reload)를 요청합니다!")
        
        async def trigger_deploy(ts):
            try:
                import websockets
                async with websockets.connect("ws://127.0.0.1:8080/ws") as ws:
                    req = {
                        "type": "deploy_model",
                        "timestamp": ts
                    }
                    await ws.send(json.dumps(req))
                    res_str = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    res = json.loads(res_str)
                    if res.get('success'):
                        print("✅ [자동 배포 성공] " + res.get('msg', ''))
                    else:
                        print("❌ [자동 배포 실패] " + res.get('msg', ''))
            except Exception as e:
                print(f"⚠️ 봇 웹소켓 통신 실패 (봇이 꺼져있을 수 있음): {e}")
                
        asyncio.run(trigger_deploy(best_ts))
    else:
        print(f"🛡 기존 모델의 성능이 더 우수하거나 새 최고 모델이 생성되지 않아 배포를 취소합니다.")


