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

    def __init__(self, db_path='data/stock_data.db', model_output_path='lgbm_model.txt', on_progress=None, on_finished=None):
        super().__init__()
        self.db_path = db_path
        self.model_output_path = model_output_path
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # 콜백 기반 시그널 초기화
        self.finished_signal = CallbackSignal()
        if on_finished:
            self.finished_signal.connect(on_finished)
        self.progress_signal = CallbackSignal()
        if on_progress:
            self.progress_signal.connect(on_progress)
        
        # 학습 파라미터 (스캘핑용 과적합 방지 설정 적용)
        self.params = {
            'objective': 'binary',              # 이진 분류 (상승/하락)
            'metric': 'auc',                    # 평가지표
            'boosting_type': 'gbdt',
            'num_leaves': 32,                   # 리프 노드 수 (32~64 권장)
            'max_depth': 6,                     # 트리 깊이 (5~7로 얕게 제한)
            'learning_rate': 0.05,
            'feature_fraction': 0.9,
            'bagging_fraction': 0.8,
            'bagging_freq': 5,
            'verbose': -1,
            'min_data_in_leaf': 200,            # 잎 하나당 최소 데이터 200개 (노이즈 방지)
            'force_col_wise': True
        }

    def run(self):
        """스레드 실행 메인 함수"""
        if not LGBM_AVAILABLE:
            error_msg = "LightGBM이 설치되지 않았습니다. 터미널에서 'pip install lightgbm'을 실행하세요."
            self.logger.error(error_msg)
            self.finished_signal.emit(False, error_msg)
            return

        conn = None
        try:
            self.progress_signal.emit("🔍 [ML] 학습 데이터 로딩 중 (DB)...")
            
            if not os.path.exists(self.db_path):
                self.finished_signal.emit(False, f"데이터베이스 파일이 없습니다: {self.db_path}")
                return

            conn = sqlite3.connect(self.db_path)
            
            # 학습에 필요한 핵심 컬럼만 조회 (데이터 양이 많을 수 있으므로 필요한 것만)
            # 주의: 데이터가 충분히 쌓인 후에 실행해야 함
            query = """
                SELECT 
                    code, datetime,
                    tic_close, tic_volume, tic_strength, 
                    tic_velocity, 
                    tic_order_book_imbalance, 
                    min3_relative_position
                FROM stock_data 
                WHERE tic_velocity IS NOT NULL 
                  AND tic_velocity != 0
                ORDER BY code, datetime
            """
            
            df = pd.read_sql(query, conn)
            
            if df.empty:
                self.finished_signal.emit(False, "[ML] 학습할 데이터가 부족하여 학습을 건너뜁니다.")
                return

            self.progress_signal.emit(f"🔍 [ML] 데이터 전처리 중... (Rows: {len(df)})")

            # === Feature Engineering (비율/속도 변환) ===
            # 종목별로 그룹화하여 계산 필요
            
            # 1. Target 생성 (미래 수익률이 수수료+세금을 상회하면 1, 아니면 0)
            # 키움증권 기준 수수료 및 세금 왕복 약 0.21% (안전하게 0.23% 이상 상승 시 1로 간주)
            FEE_RATE = 0.0023
            df['target'] = df.groupby('code')['tic_close'].shift(-5)
            # target이 NaN인 마지막 5개 행은 평가할 수 없으므로 미리 제거
            df = df.dropna(subset=['target']).copy()
            df['label'] = (df['target'] > (df['tic_close'] * (1 + FEE_RATE))).astype(int)
            
            # 2. 비율 변환 (가격 자체는 제거)
            # 이미 RELATIVE_POSITION(이격도), STRENGTH(체결강도) 등은 비율임.
            # 추가적으로 필요한 것들 변환
            
            # 순간 거래량 폭발력 (Instant Volume Spike)
            # (현재 60틱 거래량) / (직전 10개 봉 평균 거래량)
            # 1. 직전 10개 봉의 평균 구하기 (shift(1) 후 rolling(10_mean))
            df['prev_10_vol_avg'] = df.groupby('code')['tic_volume'].shift(1).rolling(10).mean()
            
            # 2. Spike 계산 (0으로 나누기 방지)
            df['tic_volume_spike'] = np.where(df['prev_10_vol_avg'] > 0, df['tic_volume'] / df['prev_10_vol_avg'], 0)
            
            # Feature 목록 정의
            features = [
                'tic_strength', 
                'tic_velocity', 
                'tic_order_book_imbalance', 
                'min3_relative_position',
                'tic_volume_spike'
            ]
            
            # 결측치 제거
            df_train = df.dropna(subset=features + ['label'])
            
            if len(df_train) < 1000:
                self.finished_signal.emit(False, f"[ML] 유효한 학습 데이터가 너무 적습니다 ({len(df_train)}개). 최소 1000개 필요.")
                return
            
            X = df_train[features]
            y = df_train['label']
            
            # 학습용/검증용 분리 (8:2)
            split_idx = int(len(X) * 0.8)
            X_train, X_val = X.iloc[:split_idx], X.iloc[split_idx:]
            y_train, y_val = y.iloc[:split_idx], y.iloc[split_idx:]
            
            # LightGBM 데이터셋 생성
            train_data = lgb.Dataset(X_train, label=y_train)
            val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)
            
            self.progress_signal.emit("🤖 [ML] LightGBM 모델 학습 시작...")
            
            # 모델 학습
            model = lgb.train(
                self.params,
                train_data,
                num_boost_round=500,        # 반복 횟수
                valid_sets=[train_data, val_data],
                callbacks=[
                    lgb.early_stopping(stopping_rounds=50), # 과적합 방지: 50번 동안 성능 향상 없으면 중단
                    lgb.log_evaluation(period=50) 
                ]
            )
            
            self.progress_signal.emit("💾 [ML] 모델 파일 저장 중...")
            model.save_model(self.model_output_path)
            
            # 검증 성능(AUC) 가져오기
            best_score = model.best_score.get('valid_1', {}).get('auc', 0.0) if hasattr(model, 'best_score') else 0.0
            
            # Feature Importance 로깅
            importance = list(zip(features, model.feature_importance()))
            importance.sort(key=lambda x: x[1], reverse=True)
            top_features = ", ".join([f"{f}:{score}" for f, score in importance[:3]])
            
            success_msg = f"✅ 모델 학습 완료! (Data: {len(df_train)}, 검증 AUC: {best_score:.4f}, Top: {top_features})"
            self.logger.info(success_msg)
            self.finished_signal.emit(True, success_msg)

        except Exception as ex:
            self.logger.error(f"모델 학습 중 치명적 오류: {ex}", exc_info=True)
            self.finished_signal.emit(False, f"학습 중 오류 발생: {ex}")
            
        finally:
            if conn:
                conn.close()

if __name__ == "__main__":
    # 단독 실행 모드
    print("🚀 [Standalone] ML 학습 프로세스 시작...")
    
    def on_progress(msg):
        print(msg)
        
    def on_finished(success, msg):
        print(f"\n결과: {'✅ 성공' if success else '❌ 실패'}")
        print(f"메시지: {msg}")
        
    worker = MLTrainingWorker(on_progress=on_progress, on_finished=on_finished)
    worker.start()
    worker.join()

