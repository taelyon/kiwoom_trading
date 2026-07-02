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
                WHERE tic_velocity IS NOT NULL 
                  AND tic_velocity != 0
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

            # === Feature Engineering (비율/속도 변환) ===
            # 종목별로 그룹화하여 계산 필요
            
            # 1. Target 생성 (향후 N틱 내 최고가가 수수료+슬리피지를 커버할 만큼 상승했는지)
            # 기존: 정확히 5틱 뒤 종가만 비교 → 실전과 괴리 (5틱 뒤에 내려와도 중간에 올랐으면 익절 가능)
            # 개선: 향후 20틱 중 최고가가 목표 수익률을 한 번이라도 돌파했으면 1로 학습
            LOOKAHEAD = 20  # 향후 20틱 (약 3~10분)
            TARGET_MARGIN = 0.003  # 0.3% (왕복 수수료 0.21% + 최소 마진)
            
            # 종목별 shift를 사용하여 향후 1~20틱의 종가를 수집한 뒤 최고가 산출
            future_shifts = [df.groupby('code')['tic_close'].shift(-i) for i in range(1, LOOKAHEAD + 1)]
            df['future_max'] = pd.concat(future_shifts, axis=1).max(axis=1)
            df = df.dropna(subset=['future_max']).copy()
            df['label'] = (df['future_max'] > (df['tic_close'] * (1 + TARGET_MARGIN))).astype(int)
            
            # 2. 비율 변환 (가격 자체는 제거)
            # 이미 RELATIVE_POSITION(이격도), STRENGTH(체결강도) 등은 비율임.
            # 추가적으로 필요한 것들 변환
            
            # [B] tic_velocity 로그 정규화 (극단적 스케일 0~999999 해소)
            df['tic_velocity'] = np.log1p(df['tic_velocity'])
            
            # 순간 거래량 폭발력 (Instant Volume Spike)
            # (현재 60틱 거래량) / (직전 10개 봉 평균 거래량)
            # 1. 직전 10개 봉의 평균 구하기 (shift(1) 후 rolling(10_mean))
            df['prev_10_vol_avg'] = df.groupby('code')['tic_volume'].shift(1).rolling(10).mean()
            
            # 2. Spike 계산 (0으로 나누기 방지)
            df['tic_volume_spike'] = np.where(df['prev_10_vol_avg'] > 0, df['tic_volume'] / df['prev_10_vol_avg'], 0)
            
            # (삭제됨) 거래 대금 가속도는 거래량 폭발력과 중복도가 높아 제거

            import talib
            def calc_new_indicators(g):
                close = g['tic_close'].values
                high = g['tic_high'].values
                low = g['tic_low'].values
                vol = g['tic_volume'].values
                
                # [C] Rolling VWAP Distance (누적 VWAP → 최근 60봉 Rolling VWAP로 교체)
                # 누적 VWAP는 오후로 갈수록 둔감해져 단타에 부적합
                VWAP_WINDOW = 60  # 최근 60봉 (약 1시간)
                typ = (high + low + close) / 3
                rolling_pv = pd.Series(typ * vol).rolling(VWAP_WINDOW, min_periods=1).sum().values
                rolling_v = pd.Series(vol).rolling(VWAP_WINDOW, min_periods=1).sum().values
                vwap = np.where(rolling_v > 0, rolling_pv / rolling_v, close)
                g['tic_vwap_distance'] = np.where(vwap > 0, (close - vwap) / vwap, 0)
                
                # (삭제됨) BB Position
                
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
            parsed_time = pd.to_datetime(df['datetime'], errors='coerce')
            parsed_time = parsed_time.fillna(pd.to_datetime('2000-01-01 12:00:00'))
            df['time_of_day_minute'] = (parsed_time.dt.hour * 60 + parsed_time.dt.minute) - (9 * 60)
            df['time_of_day_minute'] = df['time_of_day_minute'].clip(lower=0, upper=390)
            
            # [신규 피처] 순간 체결강도 (tic_buy_sell_ratio)
            # 과거 데이터에는 buy_volume이 없을 수 있으므로 예외처리
            if 'tic_buy_volume' not in df.columns:
                df['tic_buy_volume'] = 0
            df['tic_buy_sell_ratio'] = np.where(df['tic_volume'] > 0, df['tic_buy_volume'] / df['tic_volume'], 0.5)
            
            # (삭제됨) 3분봉 추세 동조화는 이진값 노이즈로 작용하여 제거
            
            # [신규 피처] 2. 이동평균선 정배열 척도 (MA Ribbon Distance)
            # 단기 이평(MA5)과 장기 이평(MA20) 간의 간격 비율
            df['tic_ma_spread'] = np.where(df['tic_ma20'] > 0, (df['tic_ma5'] - df['tic_ma20']) / df['tic_ma20'], 0)
            
            df['tic_tail_ratio'] = np.where((df['tic_high'] - df['tic_low']) > 0, (df['tic_high'] - df['tic_close']) / (df['tic_high'] - df['tic_low']), 0)
            
            # [신규 피처] 5. 봉 내 가격 변동폭 비율 (tic_spread)
            df['tic_spread'] = np.where(df['tic_close'] > 0, (df['tic_high'] - df['tic_low']) / df['tic_close'], 0)
            
            # Feature 목록 정의
            base_features = [
                'tic_strength', 
                'tic_velocity', 
                'min3_relative_position',
                'tic_volume_spike'
            ]
            
            new_features = [
                'tic_vwap_distance',
                'tic_macd_hist',
                'tic_rsi',
                'time_of_day_minute',
                'tic_price_roc',      # 가격 상승 가속도
                'tic_vol_roc',        # 거래량 폭발 가속도
                'tic_ma_spread',      # [추가] 이평선 정배열 척도
                'tic_tail_ratio',     # [추가] 캔들 윗꼬리 비율
                'tic_buy_sell_ratio', # [추가] 순간 체결강도
                'tic_spread'          # [추가] 봉 내 가격 변동폭 비율
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
            top_features = ", ".join([f"{f}:{int(score)}" for f, score in importance[:3]])
            
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


