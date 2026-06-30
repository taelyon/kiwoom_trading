"""
키움 REST API 기반 전략 평가 및 지표 처리 유틸리티 모듈
크레온 플러스 API를 키움 REST API로 전면 리팩토링
"""
# 표준 라이브러리
import json
import logging
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

# 서드파티 라이브러리
import numpy as np
import pandas as pd
import talib
import os
from config_manager import EnvConfigParser

# LightGBM 로드 (전역 변수로 1회만 로드)
LGBM_MODEL = None
try:
    import lightgbm as lgb
    if os.path.exists('lgbm_model.txt'):
        LGBM_MODEL = lgb.Booster(model_file='lgbm_model.txt')
        logging.getLogger(__name__).info("🤖전략 평가용 LightGBM 모델 로드 완료 (lgbm_model.txt)")
except Exception as e:
    logging.getLogger(__name__).warning(f"⚠️ LightGBM 모델 로드 실패 또는 미설치: {e}")

def reload_model():
    """런타임에 모델을 다시 로드합니다 (웹 대시보드에서 핫 배포 시 호출됨)"""
    global LGBM_MODEL
    try:
        import lightgbm as lgb
        if os.path.exists('lgbm_model.txt'):
            LGBM_MODEL = lgb.Booster(model_file='lgbm_model.txt')
            logging.getLogger(__name__).info("🤖전략 평가용 LightGBM 모델 런타임 리로드 완료 (lgbm_model.txt)")
            return True
    except Exception as e:
        logging.getLogger(__name__).warning(f"⚠️ LightGBM 모델 런타임 리로드 실패: {e}")
    return False


import time

# ==================== 전략 평가용 안전한 globals ====================
STRATEGY_SAFE_GLOBALS = {
    'time': time,
    '__builtins__': {
        'min': min, 'max': max, 'abs': abs, 'round': round,
        'int': int, 'float': float, 'bool': bool, 'str': str,
        'sum': sum, 'all': all, 'any': any,
        'True': True, 'False': False, 'None': None,
        # len 함수를 안전하게 래핑
        'len': lambda x: len(x) if hasattr(x, '__len__') else 1
    }
}

# ==================== 전략 평가 공통 함수 ====================
def evaluate_strategies(strategies, safe_locals, code="", strategy_type=""):
    """
    전략 조건들을 평가하고 일치하는 첫 번째 전략을 반환
    
    Args:
        strategies: 평가할 전략 리스트 (각 전략은 'name'과 'content' 필드 포함)
        safe_locals: 평가에 사용할 로컬 변수 딕셔너리
        code: 종목 코드 (로깅용)
        strategy_type: 전략 타입 ("매수", "매도" 등, 로깅용)
    
    Returns:
        (bool, dict or None): (조건 충족 여부, 충족된 전략 또는 None)
    """
    logger = logging.getLogger(__name__)

    # 백테스팅 중이고, 매도 로직이며, current_profit_pct가 손절 기준 근처인 경우에만 상세 디버그
    is_sell_debug = (strategy_type == "매도" and
                    'current_profit_pct' in safe_locals and 
                    safe_locals.get('current_profit_pct', 0) < -0.6)
    
    for strategy in strategies:
        try:
            condition = strategy.get('content', '')
            if not condition:
                continue
            
            # 손절 조건 디버그용: 조건 평가 전 현재 상태 출력
            if is_sell_debug:
                current_profit = safe_locals.get('current_profit_pct', 0)
                strategy_name = strategy.get('name', '전략')
                logger.debug(f"🔍 [{code}] 전략 평가 중: {strategy_name}")
                logger.debug(f"🔍 [{code}] 조건: {condition}")
                logger.debug(f"🔍 [{code}] 현재 수익률: {current_profit:.2f}%")
                
            result = eval(condition, STRATEGY_SAFE_GLOBALS, safe_locals)
            
            # 결과가 배열인 경우 마지막 값(최신)을 사용
            if isinstance(result, (np.ndarray, list, pd.Series)):
                if len(result) > 0:
                    result = result[-1]
                else:
                    result = False

            if is_sell_debug:
                logger.debug(f"🔍 [{code}] 평가 결과: {result}")
                
            if result:
                strategy_name = strategy.get('name', '전략')
                if code:
                    logger.debug(f"{code}: {strategy_name} 조건 충족")
                return True, strategy
                
        except Exception as ex:
            strategy_name = strategy.get('name', '알 수 없는 전략')
            logger.error(f"{code} {strategy_type} 전략 '{strategy_name}' 평가 오류: {ex}", exc_info=True)
            logger.error(f"{code} 조건: {strategy.get('content', 'N/A')}")
    
    return False, None

# ==================== 지표 추출 유틸리티 ====================
class KiwoomIndicatorExtractor:
    """키움 REST API 데이터로부터 지표를 추출하는 헬퍼 클래스"""
    logger = logging.getLogger(__qualname__)
       
    @staticmethod
    def extract_chart_indicators(chart_data, allowed_indicators=None):
        """
        차트 데이터에서 기술적 지표 추출 (캐시된 지표 우선 활용)
        
        Args:
            chart_data: 차트 데이터 DataFrame
            allowed_indicators: 계산할 지표 이름 리스트 (None이면 모든 지표 계산)
        """
        try:
            if chart_data.empty:
                return {}
            
            # 기본 가격 데이터 추출 및 타입 변환 (안전하게)
            try:
                close = np.array(chart_data['close'].values, dtype=np.float64)
                high = np.array(chart_data['high'].values, dtype=np.float64)
                low = np.array(chart_data['low'].values, dtype=np.float64)
                open_price = np.array(chart_data['open'].values, dtype=np.float64)
                volume = np.array(chart_data['volume'].values, dtype=np.float64)
                # 'strength' 컬럼이 없을 경우를 안전하게 처리
                if 'strength' in chart_data.columns:
                    strength = np.array(chart_data['strength'].values, dtype=np.float64)
                else:
                    strength = np.zeros(len(close), dtype=np.float64)
            except Exception as type_error:
                KiwoomIndicatorExtractor.logger.error(f"가격 데이터 타입 변환 실패: {type_error}", exc_info=True)
                # 타입 변환 실패 시 강제 변환 시도
                close = pd.to_numeric(chart_data['close'], errors='coerce').fillna(0).values.astype(np.float64)
                high = pd.to_numeric(chart_data['high'], errors='coerce').fillna(0).values.astype(np.float64)
                low = pd.to_numeric(chart_data['low'], errors='coerce').fillna(0).values.astype(np.float64)
                open_price = pd.to_numeric(chart_data['open'], errors='coerce').fillna(0).values.astype(np.float64)
                volume = pd.to_numeric(chart_data['volume'], errors='coerce').fillna(0).values.astype(np.float64)
                strength_series = chart_data['strength'] if 'strength' in chart_data.columns else pd.Series([0] * len(close))
                strength = pd.to_numeric(strength_series, errors='coerce').fillna(0).values.astype(np.float64)

            # inf, nan 등 이상값 정제 (talib C 확장 모듈의 segfault/무한루프 방지)
            close = np.nan_to_num(close, nan=0.0, posinf=0.0, neginf=0.0)
            high = np.nan_to_num(high, nan=0.0, posinf=0.0, neginf=0.0)
            low = np.nan_to_num(low, nan=0.0, posinf=0.0, neginf=0.0)
            open_price = np.nan_to_num(open_price, nan=0.0, posinf=0.0, neginf=0.0)
            volume = np.nan_to_num(volume, nan=0.0, posinf=0.0, neginf=0.0)
            strength = np.nan_to_num(strength, nan=0.0, posinf=0.0, neginf=0.0)

            indicators = {}

            def is_target(key):
                if allowed_indicators is None: return True
                return key in allowed_indicators

            # 1. 캐시된 지표가 있으면 그대로 사용
            cached_indicator_keys = [
                'MA5', 'MA10', 'MA20', 'MA60', 'RSI', 'MACD', 'MACD_SIGNAL', 'MACD_HIST',
                'STOCHK', 'STOCHD', 'WILLIAMS_R', 'ROC', 'OBV', 'OBV_MA20',
                'BB_UPPER', 'BB_MIDDLE', 'BB_LOWER', 'ATR',
                'TICK_VELOCITY', 'LAST_TIC_CNT'
            ]
            for key in cached_indicator_keys:
                if is_target(key) and key in chart_data.columns:
                    indicators[key] = chart_data[key].values
                    if len(indicators[key]) > 0 and not np.all(np.isnan(indicators[key])):
                        indicators[key] = chart_data[key].values

            # Alias handling for VELOCITY -> TICK_VELOCITY
            if is_target('VELOCITY') and 'TICK_VELOCITY' in chart_data.columns and 'VELOCITY' not in indicators:
                 indicators['VELOCITY'] = chart_data['TICK_VELOCITY'].values

            # 틱봉 전용 지표 백필 (Backfill)
            # TICK_VELOCITY, LAST_TIC_CNT
            if is_target('LAST_TIC_CNT') and ('LAST_TIC_CNT' not in indicators or np.all(indicators['LAST_TIC_CNT'] == 0)):
                 # 과거 데이터는 모두 완성된 봉이므로 60으로 설정 (60틱 차트 가정)
                 indicators['LAST_TIC_CNT'] = np.full(len(close), 60, dtype=int)
            
            if is_target('TICK_VELOCITY') and ('TICK_VELOCITY' not in indicators or np.all(indicators['TICK_VELOCITY'] == 0)):
                 # 시간 데이터가 있는지 확인
                 if 'time' in chart_data.columns:
                     try:
                            # 문자열 -> datetime 변환
                         # chart_data['time']이 이미 datetime 객체일 수도 있고 문자열일 수도 있음
                         times = chart_data['time']
                         if len(times) > 0:
                            if isinstance(times.iloc[0], str):
                                pd_times = pd.to_datetime(times, format='%Y%m%d%H%M%S', errors='coerce')
                            else:
                                pd_times = pd.to_datetime(times)
                            
                            # Series의 경우 .dt 접근자 사용이 올바름
                            diffs = pd_times.diff().dt.total_seconds() * 1000
                            velocities = (diffs / 6.0).fillna(0).values
                            indicators['TICK_VELOCITY'] = velocities
                     except Exception as ex:
                         KiwoomIndicatorExtractor.logger.debug(f"TICK_VELOCITY 백필 실패: {ex}")

            # 2. 캐시에 없는 지표만 재계산

            # 이동평균선
            if is_target('MA5') and 'MA5' not in indicators:
                indicators['MA5'] = talib.SMA(close, timeperiod=5) if len(close) >= 5 else np.full(len(close), np.nan)
            if is_target('MA10') and 'MA10' not in indicators:
                indicators['MA10'] = talib.SMA(close, timeperiod=10) if len(close) >= 10 else np.full(len(close), np.nan)
            if is_target('MA20') and 'MA20' not in indicators:
                indicators['MA20'] = talib.SMA(close, timeperiod=20) if len(close) >= 20 else np.full(len(close), np.nan)
            if is_target('MA60') and 'MA60' not in indicators:
                indicators['MA60'] = talib.SMA(close, timeperiod=60) if len(close) >= 60 else np.full(len(close), np.nan)

            # 상대적 이격도 (Relative Position): (현재가 - 20이평선) / 20이평선
            if is_target('RELATIVE_POSITION') and 'MA20' in indicators:
                ma20 = indicators['MA20']
                # 0으로 나누기 방지
                safe_ma20 = np.where(ma20 == 0, np.nan, ma20)
                indicators['RELATIVE_POSITION'] = (close - safe_ma20) / safe_ma20
            elif is_target('RELATIVE_POSITION'):
                indicators['RELATIVE_POSITION'] = np.full(len(close), np.nan)

            # RSI
            if is_target('RSI') and 'RSI' not in indicators:
                indicators['RSI'] = talib.RSI(close, timeperiod=14) if len(close) >= 14 else np.full(len(close), np.nan)

            if is_target('RSI_SIGNAL') and 'RSI' in indicators and 'RSI_SIGNAL' not in indicators and len(indicators['RSI']) >= 5:
                indicators['RSI_SIGNAL'] = talib.SMA(indicators['RSI'], timeperiod=5)

            # MACD
            if (is_target('MACD') or is_target('MACD_SIGNAL') or is_target('MACD_HIST')) and 'MACD' not in indicators:
                indicators['MACD'], indicators['MACD_SIGNAL'], indicators['MACD_HIST'] = (talib.MACD(close) if len(close) >= 26 
                                                                                        else (np.full(len(close), np.nan), np.full(len(close), np.nan), np.full(len(close), np.nan)))

            # 스토캐스틱
            if (is_target('STOCHK') or is_target('STOCHD')) and 'STOCHK' not in indicators and len(high) >= 14 and len(low) >= 14:
                indicators['STOCHK'], indicators['STOCHD'] = talib.STOCH(high, low, close)

            # 볼린저 밴드
            if (is_target('BB_UPPER') or is_target('BB_MIDDLE') or is_target('BB_LOWER')) and 'BB_UPPER' not in indicators and len(close) >= 20:
                bb_upper, bb_middle, bb_lower = talib.BBANDS(close, timeperiod=20)
                indicators['BB_UPPER'] = bb_upper
                indicators['BB_MIDDLE'] = bb_middle
                indicators['BB_LOWER'] = bb_lower

            # BB_POSITION과 BB_BANDWIDTH는 항상 재계산 (다른 BB 지표에 의존)
            if (is_target('BB_POSITION') or is_target('BB_BANDWIDTH')) and 'BB_UPPER' in indicators and 'BB_LOWER' in indicators and 'BB_MIDDLE' in indicators:
                bb_upper = indicators['BB_UPPER']
                bb_middle = indicators['BB_MIDDLE']
                bb_lower = indicators['BB_LOWER']
                bb_range = bb_upper - bb_lower
                safe_bb_range = np.where(bb_range == 0, 1e-9, bb_range)
                if is_target('BB_POSITION'):
                    indicators['BB_POSITION'] = (close - bb_lower) / safe_bb_range
                if is_target('BB_BANDWIDTH'):
                    safe_bb_middle = np.where(bb_middle == 0, 1e-9, bb_middle)
                    indicators['BB_BANDWIDTH'] = bb_range / safe_bb_middle

            # ATR (Average True Range)
            if is_target('ATR') and 'ATR' not in indicators and len(high) >= 14 and len(low) >= 14:
                indicators['ATR'] = talib.ATR(high, low, close, timeperiod=14)

            # Williams %R
            if is_target('WILLIAMS_R') and 'WILLIAMS_R' not in indicators and len(high) >= 14 and len(low) >= 14:
                indicators['WILLIAMS_R'] = talib.WILLR(high, low, close, timeperiod=14)

            # ROC (Rate of Change)
            if is_target('ROC') and 'ROC' not in indicators and len(close) >= 12:
                indicators['ROC'] = talib.ROC(close, timeperiod=12)

            # OBV (On Balance Volume)
            if (is_target('OBV') or is_target('OBV_MA20')) and 'OBV' not in indicators and len(close) >= 1 and len(volume) >= 1:
                obv = talib.OBV(close, volume)
                indicators['OBV'] = obv
            if is_target('OBV_MA20') and 'OBV' in indicators and 'OBV_MA20' not in indicators and len(indicators['OBV']) >= 20:
                indicators['OBV_MA20'] = talib.SMA(indicators['OBV'], timeperiod=20)
            
            # VWAP 계산
            if is_target('VWAP') and len(close) >= 1 and len(volume) >= 1:
                typical_price = (high + low + close) / 3
                
                # 누적 VWAP를 배열 형태로 계산
                cumulative_pv = np.cumsum(typical_price * volume)
                cumulative_volume = np.cumsum(volume)
                safe_cumulative_volume = np.where(cumulative_volume == 0, 1e-9, cumulative_volume)
                vwap_array = cumulative_pv / safe_cumulative_volume
                indicators['VWAP'] = vwap_array
            
            # 가격 정보 (배열 형태로 저장)
            indicators['close'] = close
            indicators['high'] = high
            indicators['low'] = low
            indicators['open'] = open_price
            indicators['volume'] = volume
            indicators['strength'] = strength
            
            return indicators
            
        except Exception as ex:
            import traceback
            KiwoomIndicatorExtractor.logger.error(f"차트 지표 추출 실패: {ex}", exc_info=True)
            return {}
    
    @staticmethod
    def calculate_additional_indicators(indicators, chart_data):
        """추가 지표 계산 (실제 전략에서 사용되는 지표만)"""
        logger = logging.getLogger(__name__)
        try:
            additional = {}
            
            # ROC_recent: 실제 전략에서 사용 중 (buy_stg_12)
            if 'ROC' in indicators:
                roc_array = indicators.get('ROC')
                if isinstance(roc_array, np.ndarray) and len(roc_array) > 0:
                    roc_recent = roc_array[-30:].tolist()  # 최근 30개
                    additional['ROC_recent'] = roc_recent
                else:
                    additional['ROC_recent'] = []
            
            # is_pullback의 기본값을 False로 설정
            additional['is_pullback'] = False
            
            # is_pullback: 최근 고점 대비 하락 여부 (buy_stg_눌림목)
            if 'tic_high' in indicators:
                high_array = indicators.get('tic_high')
                if isinstance(high_array, np.ndarray) and len(high_array) > 1:
                    recent_highs = high_array[-30:-1] # 현재 봉 제외 최근 30개
                    if len(recent_highs) > 0:
                        highest_recent = np.max(recent_highs)
                        current_close = indicators.get('tic_close', [0])[-1]
                        # 현재 종가가 최근 고점보다 낮으면 눌림목으로 간주
                        additional['is_pullback'] = current_close < highest_recent
            
            return additional
            
        except Exception as ex:
            logger.error(f"추가 지표 계산 실패: {ex}", exc_info=True)
            return {}

# ==================== 전략 평가용 로컬 변수 빌더 ====================
def prepare_buy_strategy_locals(code, tic_chart_data, min_chart_data, portfolio_info=None, realtime_metrics=None):
    """매수 로직 평가를 위한 로컬 변수 생성"""
    logger = logging.getLogger(__name__)
    try:
        if tic_chart_data.empty:
            return {}
        
        # 기본 지표 추출
        tic_indicators = KiwoomIndicatorExtractor.extract_chart_indicators(tic_chart_data)
        min_indicators = KiwoomIndicatorExtractor.extract_chart_indicators(min_chart_data)
        additional = KiwoomIndicatorExtractor.calculate_additional_indicators(tic_indicators, tic_chart_data)
        
        # 로컬 변수 딕셔너리 생성
        locals_dict = {}
        locals_dict.update(additional)

        # 틱 데이터 기반 지표에 'tic_' 접두사 추가
        for key, value in tic_indicators.items():
            # 배열 전체를 저장하여 전략에서 tic_close[-1] 형태로 접근 가능하도록 함
            if isinstance(value, np.ndarray):
                locals_dict[f'tic_{key}'] = value

        # 분봉 데이터 기반 지표에 'min3_' 접두사 추가
        for key, value in min_indicators.items():
            if isinstance(value, np.ndarray):
                locals_dict[f'min3_{key}'] = value
                
        # 기본 OHLCV 등 원본 컬럼명 소문자로 그대로 주입 (tic_ 및 min3_ 접두사 추가)
        base_columns = ['open', 'high', 'low', 'close', 'volume', 'strength']
        if not tic_chart_data.empty:
            for col in tic_chart_data.columns:
                if col in base_columns:
                    locals_dict[f'tic_{col}'] = tic_chart_data[col].values
        
        if not min_chart_data.empty:
            for col in min_chart_data.columns:
                if col in base_columns:
                    locals_dict[f'min3_{col}'] = min_chart_data[col].values
        
        # 포트폴리오 정보 추가
        if portfolio_info:
            locals_dict.update(portfolio_info)
        
        # 실시간 메트릭(Tick Velocity 등) 추가
        # 실시간 메트릭(Tick Velocity 등) 추가
        # TICK_VELOCITY (배열 보장 및 Alias 설정)
        tv_array = None
        if not tic_chart_data.empty and 'TICK_VELOCITY' in tic_chart_data.columns:
            tv_array = tic_chart_data['TICK_VELOCITY'].values
        elif realtime_metrics and 'tick_velocity' in realtime_metrics:
            tv_array = np.array([realtime_metrics['tick_velocity']])
        else:
            tv_array = np.array([999999.0])
        
        locals_dict['TICK_VELOCITY'] = tv_array
        locals_dict['tic_velocity'] = tv_array

        # ORDER_BOOK_IMBALANCE는 삭제되었으나, 이전 AI 모델 하위 호환성을 위해 0.0 고정 배열 주입
        obi_array = np.array([0.0])
            
        locals_dict['ORDER_BOOK_IMBALANCE'] = obi_array
        locals_dict['tic_order_book_imbalance'] = obi_array
        
        # min3_RELATIVE_POSITION -> min3_relative_position 매핑 (이미 배열임)
        if 'min3_RELATIVE_POSITION' in locals_dict:
            locals_dict['min3_relative_position'] = locals_dict['min3_RELATIVE_POSITION']
        
        # 기존 데이터프레임에 있는 'tic_', 'min3_' 접두사 컬럼들도 로컬 변수에 추가 (백테스팅 호환성)
        # DB에서 로드된 데이터는 이미 계산된 지표를 포함할 수 있음
        for col in tic_chart_data.columns:
            if col.startswith('tic_') or col.startswith('min3_'):
                locals_dict[col] = tic_chart_data[col].values
                
                # 대소문자 호환성을 위해 대문자 버전도 추가 (예: min3_ma20 -> min3_MA20)
                # 접두사(tic_, min3_)는 소문자 유지, 나머지 지표명은 대문자로 변환
                if col.startswith('tic_'):
                    upper_key = 'tic_' + col[4:].upper()
                    if upper_key != col:
                        locals_dict[upper_key] = tic_chart_data[col].values
                elif col.startswith('min3_'):
                    upper_key = 'min3_' + col[5:].upper()
                    if upper_key != col:
                        locals_dict[upper_key] = tic_chart_data[col].values
        
        # 백테스팅 특화 변수들
        locals_dict['code'] = code
        locals_dict['current_time'] = datetime.now()
        
        # 거래량 관련 변수
        if not tic_chart_data.empty:
            volume_series = tic_chart_data['volume']
            if len(volume_series) > 0:
                locals_dict['avg_volume'] = float(volume_series.mean())
                locals_dict['volume_ratio'] = float(volume_series.iloc[-1]) / locals_dict['avg_volume'] if locals_dict['avg_volume'] > 0 else 1.0
                
                # 최근 10개 틱의 평균 거래량 (현재 포함)
                if len(volume_series) >= 10:
                    locals_dict['tic_avg_volume_10'] = volume_series.tail(10).mean()
                    
                # 순간 거래량 폭발력 (Instant Volume Spike)
                # (현재 틱 거래량) / (직전 10개 봉 평균 거래량)
                if len(volume_series) >= 11:
                    prev_10_avg = volume_series.iloc[-11:-1].mean()
                    if prev_10_avg > 0:
                        locals_dict['tic_volume_spike'] = volume_series.iloc[-1] / prev_10_avg
                    else:
                        locals_dict['tic_volume_spike'] = 0.0
                else:
                    locals_dict['tic_volume_spike'] = 0.0

                if len(volume_series) >= 5:
                    # 최근 5개 틱의 평균 거래량 (순간 체결량 포착용)
                    locals_dict['tic_avg_volume_5'] = volume_series.tail(5).mean()

        # ==========================================================
        # AI 실시간 추론 (LightGBM Inference)
        # ==========================================================
        if LGBM_MODEL and 'TICK_VELOCITY' in locals_dict:
            try:
                # 입력 벡터 준비 (학습 때와 동일한 순서여야 함: strength, velocity, imbalance, relative_pos)
                # 데이터가 없으면 기본값 0 처리
                feature_strength = locals_dict.get('tic_strength', [0])[-1] if isinstance(locals_dict.get('tic_strength'), (list, np.ndarray)) else 0
                
                # TICK_VELOCITY (배열 보장됨)
                tv_val = locals_dict.get('tic_velocity')
                feature_velocity = tv_val[-1] if isinstance(tv_val, (list, np.ndarray)) and len(tv_val) > 0 else 999999.0

                # ORDER_BOOK_IMBALANCE (배열 보장됨)
                obi_val = locals_dict.get('tic_order_book_imbalance')
                feature_imbalance = obi_val[-1] if isinstance(obi_val, (list, np.ndarray)) and len(obi_val) > 0 else 0.0

                feature_relative = locals_dict.get('min3_relative_position', [0])[-1] if isinstance(locals_dict.get('min3_relative_position'), (list, np.ndarray)) else 0
                
                # Volume Spike (스칼라 값)
                feature_spike = locals_dict.get('tic_volume_spike', 0.0)
                
                

                # 추가 피처 4개 (VWAP, BB, MACD, RSI)
                vwap_val = locals_dict.get('tic_VWAP', [0])
                vwap_last = vwap_val[-1] if isinstance(vwap_val, (list, np.ndarray)) and len(vwap_val) > 0 else 0.0
                close_val = locals_dict.get('tic_close', [0])
                close_last = close_val[-1] if isinstance(close_val, (list, np.ndarray)) and len(close_val) > 0 else 0.0
                feature_vwap_dist = (close_last - vwap_last) / vwap_last if vwap_last > 0 else 0.0
                
                bb_pos_val = locals_dict.get('tic_BB_POSITION', [0.5])
                feature_bb_pos = bb_pos_val[-1] if isinstance(bb_pos_val, (list, np.ndarray)) and len(bb_pos_val) > 0 else 0.5
                
                macd_hist_val = locals_dict.get('tic_MACD_HIST', [0.0])
                feature_macd_hist = macd_hist_val[-1] if isinstance(macd_hist_val, (list, np.ndarray)) and len(macd_hist_val) > 0 else 0.0
                
                rsi_val = locals_dict.get('tic_RSI', [50.0])
                feature_rsi = rsi_val[-1] if isinstance(rsi_val, (list, np.ndarray)) and len(rsi_val) > 0 else 50.0
                
                # 시간 지표 (글로벌 datetime 활용)
                now = datetime.now()
                feature_time = max(0, min(390, (now.hour * 60 + now.minute) - (9 * 60)))
                
                # [신규 추가] 파생 가속도 지표 (price_roc, vol_roc) 즉석 계산
                close_arr = locals_dict.get('tic_close', [])
                if isinstance(close_arr, (list, np.ndarray)) and len(close_arr) >= 11:
                    feature_price_roc = (close_arr[-1] - close_arr[-11]) / close_arr[-11] if close_arr[-11] > 0 else 0.0
                else:
                    feature_price_roc = 0.0
                    
                vol_arr = locals_dict.get('tic_volume', [])
                if isinstance(vol_arr, (list, np.ndarray)) and len(vol_arr) >= 10:
                    vol_sum_recent_5 = sum(vol_arr[-5:])
                    vol_sum_prev_5 = sum(vol_arr[-10:-5])
                    feature_vol_roc = (vol_sum_recent_5 / vol_sum_prev_5) if vol_sum_prev_5 > 0 else 1.0
                else:
                    feature_vol_roc = 1.0
                
                # [신규 추가] 4개의 파생 피처
                min3_ma5 = locals_dict.get('min3_MA5', [0])
                min3_ma20 = locals_dict.get('min3_MA20', [0])
                feature_min3_trend_agree = 1 if (len(min3_ma5) > 0 and len(min3_ma20) > 0 and min3_ma5[-1] > min3_ma20[-1] and min3_ma20[-1] > 0) else 0
                
                tic_ma5 = locals_dict.get('tic_MA5', [0])
                tic_ma20 = locals_dict.get('tic_MA20', [0])
                feature_tic_ma_spread = (tic_ma5[-1] - tic_ma20[-1]) / tic_ma20[-1] if (len(tic_ma5) > 0 and len(tic_ma20) > 0 and tic_ma20[-1] > 0) else 0.0
                
                if isinstance(close_arr, (list, np.ndarray)) and isinstance(vol_arr, (list, np.ndarray)) and len(close_arr) >= 11 and len(vol_arr) >= 11:
                    tic_amount = [c * v for c, v in zip(close_arr[-11:], vol_arr[-11:])]
                    prev_10_amt_avg = sum(tic_amount[:-1]) / 10
                    feature_tic_amount_spike = tic_amount[-1] / prev_10_amt_avg if prev_10_amt_avg > 0 else 0.0
                else:
                    feature_tic_amount_spike = 0.0
                    
                high_val = locals_dict.get('tic_high', [0])
                low_val = locals_dict.get('tic_low', [0])
                feature_tic_tail_ratio = (high_val[-1] - close_arr[-1]) / (high_val[-1] - low_val[-1]) if (len(high_val) > 0 and len(low_val) > 0 and len(close_arr) > 0 and (high_val[-1] - low_val[-1]) > 0) else 0.0
                
                # 모델 학습 시 사용된 피처 개수에 맞춰 동적으로 차원 맞추기
                num_features = LGBM_MODEL.num_feature()
                if num_features == 15:
                    # 최신 15개 피처 (4개 파생 피처 추가)
                    input_vector = np.array([[
                        feature_strength, feature_velocity, feature_relative, feature_spike,
                        feature_vwap_dist, feature_bb_pos, feature_macd_hist, feature_rsi,
                        feature_time, feature_price_roc, feature_vol_roc,
                        feature_min3_trend_agree, feature_tic_ma_spread, feature_tic_amount_spike, feature_tic_tail_ratio
                    ]])
                elif num_features == 11:
                    # 이전 11개 피처 (가속도 지표 포함)
                    input_vector = np.array([[
                        feature_strength, feature_velocity, feature_relative, feature_spike,
                        feature_vwap_dist, feature_bb_pos, feature_macd_hist, feature_rsi,
                        feature_time, feature_price_roc, feature_vol_roc
                    ]])
                elif num_features == 9:
                    # 직전 9개 피처 (시간 지표 포함)
                    input_vector = np.array([[
                        feature_strength, feature_velocity, feature_imbalance, feature_relative, feature_spike,
                        feature_vwap_dist, feature_bb_pos, feature_macd_hist, feature_rsi,
                        feature_time
                    ]])
                elif num_features == 8:
                    # 직전 8개 피처
                    input_vector = np.array([[
                        feature_strength, feature_velocity, feature_relative, feature_spike,
                        feature_vwap_dist, feature_bb_pos, feature_macd_hist, feature_rsi
                    ]])
                elif num_features == 4:
                    # 직전 4개 피처
                    input_vector = np.array([[
                        feature_strength, feature_velocity, feature_relative, feature_spike
                    ]])
                elif num_features == 5:
                    input_vector = np.array([[feature_strength, feature_velocity, feature_imbalance, feature_relative, feature_spike]])
                else:
                    # 예외 발생 시 안전하게 0 리턴 처리 (크래시 방지)
                    input_vector = np.zeros((1, num_features))
                
                # 추론 실행
                ai_score = LGBM_MODEL.predict(input_vector)[0]
                locals_dict['AI_SCORE'] = float(ai_score)
                # logger.debug(f"🤖 AI 추론: {code} Score={ai_score:.4f}")
                
            except Exception as ai_ex:
                # logger.error(f"AI 추론 실패: {ai_ex}")
                locals_dict['AI_SCORE'] = 0.0
        else:
            locals_dict['AI_SCORE'] = 0.0

        return locals_dict
        
    except Exception as ex:
        logger.error(f"백테스팅 매수 로컬 변수 생성 실패 ({code}): {ex}", exc_info=True)
        return {}

def prepare_sell_strategy_locals(code, tic_chart_data, min_chart_data, buy_price, buy_time, portfolio_info=None, current_price=None, commission_rate=0.00015, tax_rate=0.0018, realtime_metrics=None):
    """매도 로직 평가를 위한 로컬 변수 생성"""
    logger = logging.getLogger(__name__)
    try:
        # 로컬 변수 딕셔너리 초기화
        locals_dict = {}
        
        # 1. 차트 지표 추출 (데이터가 있는 경우에만)
        if not tic_chart_data.empty:
            # 기본 지표 추출
            tic_indicators = KiwoomIndicatorExtractor.extract_chart_indicators(tic_chart_data)
            min_indicators = KiwoomIndicatorExtractor.extract_chart_indicators(min_chart_data)
            additional = KiwoomIndicatorExtractor.calculate_additional_indicators(tic_indicators, tic_chart_data)
            
            locals_dict.update(additional)

            # 틱 데이터 기반 지표에 'tic_' 접두사 추가
            for key, value in tic_indicators.items():
                if isinstance(value, np.ndarray):
                    locals_dict[f'tic_{key}'] = value

            # 분봉 데이터 기반 지표에 'min3_' 접두사 추가
            for key, value in min_indicators.items():
                if isinstance(value, np.ndarray):
                    locals_dict[f'min3_{key}'] = value
                    
            # 기본 OHLCV 등 원본 컬럼명 소문자로 그대로 주입 (tic_ 및 min3_ 접두사 추가)
            base_columns = ['open', 'high', 'low', 'close', 'volume', 'strength']
            if not tic_chart_data.empty:
                for col in tic_chart_data.columns:
                    if col in base_columns:
                        locals_dict[f'tic_{col}'] = tic_chart_data[col].values
            
            if not min_chart_data.empty:
                for col in min_chart_data.columns:
                    if col in base_columns:
                        locals_dict[f'min3_{col}'] = min_chart_data[col].values
            
            # 기존 데이터프레임 컬럼 추가 (백테스팅 호환성)
            for col in tic_chart_data.columns:
                if col.startswith('tic_') or col.startswith('min3_'):
                    locals_dict[col] = tic_chart_data[col].values
                    # 대문자 버전 추가
                    if col.startswith('tic_'):
                        upper_key = 'tic_' + col[4:].upper()
                        if upper_key != col:
                            locals_dict[upper_key] = tic_chart_data[col].values
                    elif col.startswith('min3_'):
                        upper_key = 'min3_' + col[5:].upper()
                        if upper_key != col:
                            locals_dict[upper_key] = tic_chart_data[col].values

        # 2. 현재가 설정
        if current_price is None or current_price == 0:
            if 'tic_close' in locals_dict and len(locals_dict['tic_close']) > 0:
                current_price = locals_dict['tic_close'][-1]
                logger.debug(f"[{code}] 매도 평가용 현재가 설정: {current_price} (tic_close[-1])")
            else:
                current_price = 0 # 현재가 없음

        # 3. 매매 관련 기본 변수 설정 (차트 데이터 유무와 무관하게 항상 설정)
        locals_dict['code'] = code
        locals_dict['buy_price'] = buy_price
        locals_dict['buy_time'] = buy_time
        locals_dict['current_price'] = current_price
        
        # 수익률 계산
        if buy_price > 0:
            buy_cost_per_share = buy_price * (1 + commission_rate)
            # 현재가가 0이면 수익률 계산 불가 (또는 -100% 처리)
            if current_price > 0:
                sell_revenue_per_share = current_price * (1 - commission_rate - tax_rate)
                net_profit_per_share = sell_revenue_per_share - buy_cost_per_share
                locals_dict['current_profit_pct'] = (net_profit_per_share / buy_cost_per_share) * 100
            else:
                locals_dict['current_profit_pct'] = 0.0
        else:
            locals_dict['current_profit_pct'] = 0.0
        
        # 보유 시간 계산
        if buy_time:
            hold_time = datetime.now() - buy_time
            locals_dict['hold_minutes'] = hold_time.total_seconds() / 60
            locals_dict['hold_hours'] = hold_time.total_seconds() / 3600
        else:
            locals_dict['hold_minutes'] = 0
            locals_dict['hold_hours'] = 0

        # 매수 후 경과된 봉 개수 계산 (차트 데이터 필요)
        if buy_time and not tic_chart_data.empty and 'time' in tic_chart_data.columns:
            try:
                tic_times = pd.to_datetime(tic_chart_data['time'])
                locals_dict['bars_since_entry'] = (tic_times > buy_time).sum()
            except Exception as e:
                logger.debug(f"[{code}] bars_since_entry 계산 실패: {e}")
                locals_dict['bars_since_entry'] = 0
        else:
            locals_dict['bars_since_entry'] = 0

        # 포트폴리오 정보 추가
        if portfolio_info:
            locals_dict.update(portfolio_info)
            
        # TICK_VELOCITY (배열 보장 및 Alias 설정)
        tv_array = None
        if not tic_chart_data.empty and 'TICK_VELOCITY' in tic_chart_data.columns:
            tv_array = tic_chart_data['TICK_VELOCITY'].values
        elif realtime_metrics and 'tick_velocity' in realtime_metrics:
            tv_array = np.array([realtime_metrics['tick_velocity']])
        else:
            tv_array = np.array([999999.0])
        
        locals_dict['TICK_VELOCITY'] = tv_array
        locals_dict['tic_velocity'] = tv_array

        # ORDER_BOOK_IMBALANCE는 삭제되었으나, 이전 AI 모델 하위 호환성을 위해 0.0 고정 배열 주입
        obi_array = np.array([0.0])
            
        locals_dict['ORDER_BOOK_IMBALANCE'] = obi_array
        locals_dict['tic_order_book_imbalance'] = obi_array
            
        # min3_RELATIVE_POSITION -> min3_relative_position 매핑
        if 'min3_RELATIVE_POSITION' in locals_dict:
            locals_dict['min3_relative_position'] = locals_dict['min3_RELATIVE_POSITION']
            
        # 최고가 추적 (들여쓰기 수정됨)
        highest_price = 0
        if portfolio_info:
            highest_price = portfolio_info.get('highest_prices', {}).get(code, 0)
        
        # 현재가가 더 높으면 최고가 갱신 (메모리상에서만, 실제 업데이트는 외부에서 처리)
        if current_price > highest_price:
            highest_price = current_price
            
        locals_dict['highest_price'] = highest_price
        
        # 최고점 대비 하락률
        if highest_price > 0:
            locals_dict['from_peak_pct'] = (current_price - highest_price) / highest_price * 100
        else:
            locals_dict['from_peak_pct'] = 0.0
        
        # 시간 관련 변수
        current_hour = datetime.now().hour
        locals_dict['after_market_close'] = current_hour >= 15  # 15시 이후 (장 마감 후)
        locals_dict['market_open'] = 9 <= current_hour <= 15  # 장 개장 시간
        
        # ==========================================================
        # AI 실시간 추론 (LightGBM Inference)
        # ==========================================================
        if LGBM_MODEL and 'TICK_VELOCITY' in locals_dict:
            try:
                # 입력 벡터 준비
                feature_strength = locals_dict.get('tic_strength', [0])[-1] if isinstance(locals_dict.get('tic_strength'), (list, np.ndarray)) else 0
                
                # TICK_VELOCITY (배열 보장됨)
                tv_val = locals_dict.get('tic_velocity')
                feature_velocity = tv_val[-1] if isinstance(tv_val, (list, np.ndarray)) and len(tv_val) > 0 else 999999.0

                # ORDER_BOOK_IMBALANCE (배열 보장됨)
                obi_val = locals_dict.get('tic_order_book_imbalance')
                feature_imbalance = obi_val[-1] if isinstance(obi_val, (list, np.ndarray)) and len(obi_val) > 0 else 0.0

                feature_relative = locals_dict.get('min3_relative_position', [0])[-1] if isinstance(locals_dict.get('min3_relative_position'), (list, np.ndarray)) else 0
                
                # Volume Spike (스칼라 값)
                feature_spike = locals_dict.get('tic_volume_spike', 0.0)
                
                # 추가 피처 (신규 모델용)
                feature_turnover = locals_dict.get('tic_turnover', [0])[-1] if isinstance(locals_dict.get('tic_turnover'), (list, np.ndarray)) else 0
                # 호가 잔량
                sell_1 = locals_dict.get('tic_sell_hoga_size_1', [0])[-1] if isinstance(locals_dict.get('tic_sell_hoga_size_1'), (list, np.ndarray)) else 0
                sell_2 = locals_dict.get('tic_sell_hoga_size_2', [0])[-1] if isinstance(locals_dict.get('tic_sell_hoga_size_2'), (list, np.ndarray)) else 0
                sell_3 = locals_dict.get('tic_sell_hoga_size_3', [0])[-1] if isinstance(locals_dict.get('tic_sell_hoga_size_3'), (list, np.ndarray)) else 0
                buy_1 = locals_dict.get('tic_buy_hoga_size_1', [0])[-1] if isinstance(locals_dict.get('tic_buy_hoga_size_1'), (list, np.ndarray)) else 0
                buy_2 = locals_dict.get('tic_buy_hoga_size_2', [0])[-1] if isinstance(locals_dict.get('tic_buy_hoga_size_2'), (list, np.ndarray)) else 0
                buy_3 = locals_dict.get('tic_buy_hoga_size_3', [0])[-1] if isinstance(locals_dict.get('tic_buy_hoga_size_3'), (list, np.ndarray)) else 0
                
                # 모델 학습 시 사용된 피처 개수에 맞춰 동적으로 차원 맞추기
                num_features = LGBM_MODEL.num_feature()
                if num_features == 14:
                    # 추가 피처 4개 (VWAP, BB, MACD, RSI)
                    vwap_val = locals_dict.get('tic_VWAP', [0])
                    vwap_last = vwap_val[-1] if isinstance(vwap_val, (list, np.ndarray)) and len(vwap_val) > 0 else 0.0
                    close_val = locals_dict.get('tic_close', [0])
                    close_last = close_val[-1] if isinstance(close_val, (list, np.ndarray)) and len(close_val) > 0 else 0.0
                    feature_vwap_dist = (close_last - vwap_last) / vwap_last if vwap_last > 0 else 0.0
                    
                    bb_pos_val = locals_dict.get('tic_BB_POSITION', [0.5])
                    feature_bb_pos = bb_pos_val[-1] if isinstance(bb_pos_val, (list, np.ndarray)) and len(bb_pos_val) > 0 else 0.5
                    
                    macd_hist_val = locals_dict.get('tic_MACD_HIST', [0.0])
                    feature_macd_hist = macd_hist_val[-1] if isinstance(macd_hist_val, (list, np.ndarray)) and len(macd_hist_val) > 0 else 0.0
                    
                    rsi_val = locals_dict.get('tic_RSI', [50.0])
                    feature_rsi = rsi_val[-1] if isinstance(rsi_val, (list, np.ndarray)) and len(rsi_val) > 0 else 50.0
                    
                    now = datetime.now()
                    feature_time = max(0, min(390, (now.hour * 60 + now.minute) - (9 * 60)))
                    
                    # [신규 추가] 파생 가속도 지표 (price_roc, vol_roc)
                    close_arr = locals_dict.get('tic_close', [])
                    if isinstance(close_arr, (list, np.ndarray)) and len(close_arr) >= 11:
                        feature_price_roc = (close_arr[-1] - close_arr[-11]) / close_arr[-11] if close_arr[-11] > 0 else 0.0
                    else:
                        feature_price_roc = 0.0
                        
                    vol_arr = locals_dict.get('tic_volume', [])
                    if isinstance(vol_arr, (list, np.ndarray)) and len(vol_arr) >= 10:
                        vol_sum_recent_5 = sum(vol_arr[-5:])
                        vol_sum_prev_5 = sum(vol_arr[-10:-5])
                        feature_vol_roc = (vol_sum_recent_5 / vol_sum_prev_5) if vol_sum_prev_5 > 0 else 1.0
                    else:
                        feature_vol_roc = 1.0

                    # [신규 추가] 4개의 파생 피처
                    min3_ma5 = locals_dict.get('min3_MA5', [0])
                    min3_ma20 = locals_dict.get('min3_MA20', [0])
                    feature_min3_trend_agree = 1 if (len(min3_ma5) > 0 and len(min3_ma20) > 0 and min3_ma5[-1] > min3_ma20[-1] and min3_ma20[-1] > 0) else 0
                    
                    tic_ma5 = locals_dict.get('tic_MA5', [0])
                    tic_ma20 = locals_dict.get('tic_MA20', [0])
                    feature_tic_ma_spread = (tic_ma5[-1] - tic_ma20[-1]) / tic_ma20[-1] if (len(tic_ma5) > 0 and len(tic_ma20) > 0 and tic_ma20[-1] > 0) else 0.0
                    
                    if isinstance(close_arr, (list, np.ndarray)) and isinstance(vol_arr, (list, np.ndarray)) and len(close_arr) >= 11 and len(vol_arr) >= 11:
                        tic_amount = [c * v for c, v in zip(close_arr[-11:], vol_arr[-11:])]
                        prev_10_amt_avg = sum(tic_amount[:-1]) / 10
                        feature_tic_amount_spike = tic_amount[-1] / prev_10_amt_avg if prev_10_amt_avg > 0 else 0.0
                    else:
                        feature_tic_amount_spike = 0.0
                        
                    high_val = locals_dict.get('tic_high', [0])
                    low_val = locals_dict.get('tic_low', [0])
                    feature_tic_tail_ratio = (high_val[-1] - close_arr[-1]) / (high_val[-1] - low_val[-1]) if (len(high_val) > 0 and len(low_val) > 0 and len(close_arr) > 0 and (high_val[-1] - low_val[-1]) > 0) else 0.0

                    if num_features == 15:
                        # 최신 15개 피처 (4개 파생 피처 추가)
                        input_vector = np.array([[
                            feature_strength, feature_velocity, feature_relative, feature_spike,
                            feature_vwap_dist, feature_bb_pos, feature_macd_hist, feature_rsi,
                            feature_time, feature_price_roc, feature_vol_roc,
                            feature_min3_trend_agree, feature_tic_ma_spread, feature_tic_amount_spike, feature_tic_tail_ratio
                        ]])
                    elif num_features == 11:
                        # 이전 11개 피처 (가속도 포함)
                        input_vector = np.array([[
                            feature_strength, feature_velocity, feature_relative, feature_spike,
                            feature_vwap_dist, feature_bb_pos, feature_macd_hist, feature_rsi,
                            feature_time, feature_price_roc, feature_vol_roc
                        ]])
                elif num_features == 5:
                    input_vector = np.array([[feature_strength, feature_velocity, feature_imbalance, feature_relative, feature_spike]])
                elif num_features == 12:
                    # 12개 피처 (기본 5 + 턴오버/호가잔량 7)
                    input_vector = np.array([[
                        feature_strength, feature_velocity, feature_imbalance, feature_relative, feature_spike,
                        feature_turnover, sell_1, sell_2, sell_3, buy_1, buy_2, buy_3
                    ]])
                else:
                    input_vector = np.array([[
                        feature_strength, feature_velocity, feature_imbalance, feature_relative, feature_spike,
                        feature_turnover, sell_1, sell_2, sell_3, buy_1, buy_2, buy_3
                    ]])
                
                # 추론 실행
                ai_score = LGBM_MODEL.predict(input_vector)[0]
                locals_dict['AI_SCORE'] = float(ai_score)
                
            except Exception as ai_ex:
                locals_dict['AI_SCORE'] = 0.0
        else:
            locals_dict['AI_SCORE'] = 0.0
        
        return locals_dict
        
    except Exception as ex:
        logger.error(f"백테스팅 매도 로컬 변수 생성 실패 ({code}): {ex}", exc_info=True)
        return {}

# ==================== 설정 파일에서 전략 로드 ====================
def load_strategies_from_config(config_file='.env'):
    """설정 파일에서 전략 로드"""
    logger = logging.getLogger(__name__)
    try:
        config = EnvConfigParser()
        
        strategies = {}
        
        # 동적으로 전략 섹션 추출 (STRATEGIES_STG_x 에서 섹션명 가져오기)
        strategy_sections = []
        if config.has_section('STRATEGIES'):
            for opt in config.options('STRATEGIES'):
                val = config.get('STRATEGIES', opt)
                if val and val not in strategy_sections:
                    strategy_sections.append(val)
                    
        # 기본값 (하위 호환성용)
        if not strategy_sections:
            strategy_sections = ['VI 발동', '급등주', '갭상승']
        
        for section in strategy_sections:
            # 섹션이 존재하는지 확인 (STRATEGY_섹션명_... 키가 하나라도 있는지)
            if config.has_section(section) or any(k.startswith(f"STRATEGY_{section}_") for k in config._data):
                strategies[section] = {
                    'buy_strategies': [],
                    'sell_strategies': []
                }
                
                # 섹션의 모든 옵션 확인
                for option in config.options(section):
                    if option.startswith('buy_stg_'):
                        try:
                            strategy_data = json.loads(config.get(section, option))
                            strategies[section]['buy_strategies'].append(strategy_data)
                        except json.JSONDecodeError:
                            logger.warning(f"매수 로직 파싱 실패: {section}.{option}")
                    
                    elif option.startswith('sell_stg_'):
                        try:
                            strategy_data = json.loads(config.get(section, option))
                            strategies[section]['sell_strategies'].append(strategy_data)
                        except json.JSONDecodeError:
                            logger.warning(f"매도 로직 파싱 실패: {section}.{option}")
        
        return strategies
        
    except Exception as ex:
        logger.error(f"전략 로드 실패: {ex}", exc_info=True)
        return {}
