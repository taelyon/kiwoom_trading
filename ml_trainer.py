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
            'feature_fraction': 0.9,
            'bagging_fraction': 0.8,
            'bagging_freq': 5,
            'verbose': -1,
            'min_data_in_leaf': 50,
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
                WHERE tic_velocity IS NOT NULL 
                  AND tic_velocity != 0
            """
            params = []
            if self.start_date:
                query += " AND datetime >= ?"
                params.append(self.start_date.replace('-', '') + '000000')
            if self.end_date:
                query += " AND datetime <= ?"
                params.append(self.end_date.replace('-', '') + '235959')
            query += " ORDER BY code, datetime"
            
            df = pd.read_sql(query, conn, params=params)
            
            if df.empty:
                self.finished_signal.emit(False, "[ML] 학습할 데이터가 부족하여 학습을 건너뜁니다.", None)
                return

            self.progress_signal.emit(f"🔍 [ML] 데이터 전처리 중... (Rows: {len(df)})")

            # === Feature Engineering (비율/속도 변환) ===
            # 종목별로 그룹화하여 계산 필요
            
            # 1. Target 생성 (미래 수익률이 수수료+세금 및 슬리피지를 상회하면 1, 아니면 0)
            # 키움증권 기준 수수료/세금 왕복 0.21% + 호가 슬리피지 방어를 위해 0.35% 이상 상승 시 1로 간주
            TARGET_MARGIN = 0.0035
            df['target'] = df.groupby('code')['tic_close'].shift(-5)
            # target이 NaN인 마지막 5개 행은 평가할 수 없으므로 미리 제거
            df = df.dropna(subset=['target']).copy()
            df['label'] = (df['target'] > (df['tic_close'] * (1 + TARGET_MARGIN))).astype(int)
            
            # 2. 비율 변환 (가격 자체는 제거)
            # 이미 RELATIVE_POSITION(이격도), STRENGTH(체결강도) 등은 비율임.
            # 추가적으로 필요한 것들 변환
            
            # 순간 거래량 폭발력 (Instant Volume Spike)
            # (현재 60틱 거래량) / (직전 10개 봉 평균 거래량)
            # 1. 직전 10개 봉의 평균 구하기 (shift(1) 후 rolling(10_mean))
            df['prev_10_vol_avg'] = df.groupby('code')['tic_volume'].shift(1).rolling(10).mean()
            
            # 2. Spike 계산 (0으로 나누기 방지)
            df['tic_volume_spike'] = np.where(df['prev_10_vol_avg'] > 0, df['tic_volume'] / df['prev_10_vol_avg'], 0)
            
            import talib
            def calc_new_indicators(g):
                close = g['tic_close'].values
                high = g['tic_high'].values
                low = g['tic_low'].values
                vol = g['tic_volume'].values
                
                # VWAP Distance
                typ = (high + low + close) / 3
                cum_pv = np.cumsum(typ * vol)
                cum_v = np.cumsum(vol)
                vwap = np.where(cum_v > 0, cum_pv / cum_v, close)
                g['tic_vwap_distance'] = np.where(vwap > 0, (close - vwap) / vwap, 0)
                
                # BB Position
                if len(close) >= 20:
                    u, m, l = talib.BBANDS(close, timeperiod=20)
                    r = u - l
                    bb_pos = np.where(r > 0, (close - l) / r, 0.5)
                    g['tic_bb_position'] = pd.Series(bb_pos).fillna(0.5).values
                else:
                    g['tic_bb_position'] = 0.5
                    
                # MACD Hist
                if len(close) >= 26:
                    _, _, h = talib.MACD(close)
                    g['tic_macd_hist'] = pd.Series(h).fillna(0.0).values
                else:
                    g['tic_macd_hist'] = 0.0
                    
                # RSI
                if len(close) >= 14:
                    g['tic_rsi'] = pd.Series(talib.RSI(close, timeperiod=14)).fillna(50.0).values
                else:
                    g['tic_rsi'] = 50.0
                    
                # [신규] 최근 10틱 가격 변화율 (가속도)
                g['tic_price_roc'] = g['tic_close'].pct_change(periods=10).fillna(0.0)
                
                # [신규] 최근 거래량 가속도 (직전 5틱 볼륨 합계 대비 최근 5틱 볼륨 합계 비)
                vol_sum_5 = g['tic_volume'].rolling(5).sum()
                prev_vol_sum_5 = vol_sum_5.shift(5)
                g['tic_vol_roc'] = pd.Series(np.where(prev_vol_sum_5 > 0, vol_sum_5 / prev_vol_sum_5, 1.0)).fillna(1.0).values
                    
                return g
            
            df = df.groupby('code', group_keys=False).apply(calc_new_indicators)
            
            # 시간 지표 추가 (장 시작 후 몇 분이 지났는지)
            parsed_time = pd.to_datetime(df['datetime'], format='%Y%m%d%H%M%S', errors='coerce')
            parsed_time = parsed_time.fillna(pd.to_datetime('20000101120000', format='%Y%m%d%H%M%S'))
            df['time_of_day_minute'] = (parsed_time.dt.hour * 60 + parsed_time.dt.minute) - (9 * 60)
            df['time_of_day_minute'] = df['time_of_day_minute'].clip(lower=0, upper=390)
            
            # Feature 목록 정의
            base_features = [
                'tic_strength', 
                'tic_velocity', 
                'min3_relative_position',
                'tic_volume_spike'
            ]
            
            new_features = [
                'tic_vi_distance', 
                'tic_kosdaq_change',
                'tic_vwap_distance',
                'tic_bb_position',
                'tic_macd_hist',
                'tic_rsi',
                'time_of_day_minute',
                'tic_price_roc',      # [추가] 가격 상승 가속도
                'tic_vol_roc'         # [추가] 거래량 폭발 가속도
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
            
            # 학습용/검증용 분리 (8:2)
            split_idx = int(len(X) * 0.8)
            X_train, X_val = X.iloc[:split_idx], X.iloc[split_idx:]
            y_train, y_val = y.iloc[:split_idx], y.iloc[split_idx:]
            
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
                num_boost_round=1500,        # 반복 횟수 (Early Stopping 의존)
                valid_sets=[train_data, val_data],
                callbacks=[
                    lgb.early_stopping(stopping_rounds=50), # 과적합 방지: 50번 동안 성능 향상 없으면 중단
                    lgb.log_evaluation(period=50) 
                ]
            )
            
            self.progress_signal.emit("💾 [ML] 모델 파일 저장 중...")
            self.model_output_path = 'lgbm_model.txt'
            model.save_model(self.model_output_path)
            
            # 검증 성능(AUC) 가져오기
            best_score = model.best_score.get('valid_1', {}).get('auc', 0.0) if hasattr(model, 'best_score') else 0.0
            best_train_score = model.best_score.get('training', {}).get('auc', 0.0) if hasattr(model, 'best_score') else 0.0
            
            # Feature Importance 로깅
            importance = list(zip(features, model.feature_importance()))
            importance.sort(key=lambda x: x[1], reverse=True)
            top_features = ", ".join([f"{f}:{score}" for f, score in importance[:3]])
            
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
            
            success_msg = f"✅ 모델 학습 완료! (Data: {len(df_train)}, 검증 AUC: {best_score:.4f}, Top: {top_features})"
            self.logger.debug(success_msg)
            self.finished_signal.emit(True, success_msg, metrics)

        except Exception as ex:
            self.logger.error(f"모델 학습 중 치명적 오류: {ex}", exc_info=True)
            self.finished_signal.emit(False, f"학습 중 오류 발생: {ex}", None)
            
        finally:
            if conn:
                conn.close()

if __name__ == "__main__":
    # 단독 실행 모드
    print("🚀 [Standalone] ML 학습 프로세스 시작...")
    
    def on_progress(msg):
        print(msg)
        
    def on_finished(success, msg, metrics=None):
        print(f"\n결과: {'✅ 성공' if success else '❌ 실패'}")
        print(f"메시지: {msg}")
        if metrics:
            print(f"Metrics: {metrics}")
        
    worker = MLTrainingWorker(on_progress=on_progress, on_finished=on_finished)
    worker.start()
    worker.join()

