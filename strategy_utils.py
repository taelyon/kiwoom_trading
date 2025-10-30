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

# ==================== 전략 평가용 안전한 globals ====================
STRATEGY_SAFE_GLOBALS = {
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
    # 매도 전략이고 current_profit_pct가 손절 기준 근처인 경우 상세 디버그
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
                logging.debug(f"🔍 [{code}] 전략 평가 중: {strategy_name}")
                logging.debug(f"🔍 [{code}] 조건: {condition}")
                logging.debug(f"🔍 [{code}] 현재 수익률: {current_profit:.2f}%")
                
            result = eval(condition, STRATEGY_SAFE_GLOBALS, safe_locals)
            
            if is_sell_debug:
                logging.debug(f"🔍 [{code}] 평가 결과: {result}")
                
            if result:
                strategy_name = strategy.get('name', '전략')
                if code:
                    logging.debug(f"{code}: {strategy_name} 조건 충족")
                return True, strategy
                
        except Exception as ex:
            strategy_name = strategy.get('name', '알 수 없는 전략')
            logging.error(f"{code} {strategy_type} 전략 '{strategy_name}' 평가 오류: {ex}")
            logging.error(f"{code} 조건: {strategy.get('content', 'N/A')}")
            logging.error(f"{code} 예외 상세: {traceback.format_exc()}")
    
    return False, None

# ==================== 지표 추출 유틸리티 ====================
class KiwoomIndicatorExtractor:
    """키움 REST API 데이터로부터 지표를 추출하는 헬퍼 클래스"""
    
    @staticmethod
    def extract_realtime_indicators(kiwoom_data):
        """키움 REST API 실시간 데이터에서 주요 지표 추출"""
        try:
            return {
                # 기본 가격 정보
                'C': kiwoom_data.get('current_price', 0),
                'O': kiwoom_data.get('open', 0),
                'H': kiwoom_data.get('high', 0),
                'L': kiwoom_data.get('low', 0),
                'V': kiwoom_data.get('volume', 0),
                
                # 변화율 정보
                'change': kiwoom_data.get('change', 0),
                'change_rate': kiwoom_data.get('change_rate', 0),
                
                # 거래량 정보
                'volume': kiwoom_data.get('volume', 0),
                'turnover': kiwoom_data.get('turnover', 0),
                
                # 시가총액 및 밸류에이션
                'market_cap': kiwoom_data.get('market_cap', 0),
                'per': kiwoom_data.get('per', 0),
                'pbr': kiwoom_data.get('pbr', 0),
                
                # 호가 정보 (있는 경우)
                'bid_price': kiwoom_data.get('bid_price', 0),
                'ask_price': kiwoom_data.get('ask_price', 0),
                'bid_volume': kiwoom_data.get('bid_volume', 0),
                'ask_volume': kiwoom_data.get('ask_volume', 0),
            }
        except Exception as ex:
            logging.error(f"실시간 지표 추출 실패: {ex}")
            return {}
    
    @staticmethod
    def extract_chart_indicators(chart_data):
        """차트 데이터에서 기술적 지표 추출 (캐시된 지표 우선 활용)"""
        try:
            if chart_data.empty:
                return {}
            
            indicators = {}
            
            # ========== 1단계: 캐시된 지표 추출 (재계산 불필요) ==========
            # chart_data의 컬럼에 이미 계산된 지표가 있는지 확인
            cached_indicator_keys = [
                'MA5', 'MA10', 'MA20', 'MA50', 'MA60', 'MA120',
                'RSI', 'MACD', 'MACD_SIGNAL', 'MACD_HIST',
                'STOCH_K', 'STOCH_D', 'WILLIAMS_R', 'ROC', 'OBV', 'OBV_MA20',
                'BB_UPPER', 'BB_MIDDLE', 'BB_LOWER', 'ATR'
            ]
            
            cached_indicators_found = 0
            for key in cached_indicator_keys:
                if key in chart_data.columns:
                    indicator_values = chart_data[key].values
                    # NaN이 아닌 유효한 값이 있는지 확인
                    if len(indicator_values) > 0 and not np.all(np.isnan(indicator_values)):
                        indicators[key] = indicator_values
                        cached_indicators_found += 1
            
            # 캐시된 지표가 충분히 있으면 기본 가격 데이터만 추가하고 반환
            if cached_indicators_found >= 10:  # 주요 지표 10개 이상 캐시되어 있으면
                logging.debug(f"✅ 캐시된 지표 {cached_indicators_found}개 활용 (재계산 생략)")
                
                # 기본 가격 데이터 추출
                close = chart_data['close'].values
                high = chart_data['high'].values
                low = chart_data['low'].values
                volume = chart_data['volume'].values
                
                # 최신 값들 추출 (스칼라 값)
                if 'MA5' in indicators and len(indicators['MA5']) > 0:
                    indicators['MA5_value'] = indicators['MA5'][-1]
                if 'MA10' in indicators and len(indicators['MA10']) > 0:
                    indicators['MA10_value'] = indicators['MA10'][-1]
                if 'MA20' in indicators and len(indicators['MA20']) > 0:
                    indicators['MA20_value'] = indicators['MA20'][-1]
                if 'MA60' in indicators and len(indicators['MA60']) > 0:
                    indicators['MA60_value'] = indicators['MA60'][-1]
                if 'RSI' in indicators and len(indicators['RSI']) > 0:
                    indicators['RSI_value'] = indicators['RSI'][-1]
                    # RSI 신호선
                    if len(indicators['RSI']) >= 5:
                        rsi_signal = talib.SMA(indicators['RSI'].astype(float), timeperiod=5)
                        indicators['RSI_SIGNAL_value'] = rsi_signal[-1] if len(rsi_signal) > 0 else 50
                    else:
                        indicators['RSI_SIGNAL_value'] = 50
                else:
                    indicators['RSI_value'] = 50
                    indicators['RSI_SIGNAL_value'] = 50
                    
                if 'MACD' in indicators and len(indicators['MACD']) > 0:
                    indicators['MACD_value'] = indicators['MACD'][-1]
                if 'MACD_SIGNAL' in indicators and len(indicators['MACD_SIGNAL']) > 0:
                    indicators['MACD_SIGNAL_value'] = indicators['MACD_SIGNAL'][-1]
                if 'MACD_HIST' in indicators and len(indicators['MACD_HIST']) > 0:
                    indicators['MACD_HIST_value'] = indicators['MACD_HIST'][-1]
                if 'STOCH_K' in indicators and len(indicators['STOCH_K']) > 0:
                    indicators['STOCHK_value'] = indicators['STOCH_K'][-1]
                if 'STOCH_D' in indicators and len(indicators['STOCH_D']) > 0:
                    indicators['STOCHD_value'] = indicators['STOCH_D'][-1]
                if 'WILLIAMS_R' in indicators and len(indicators['WILLIAMS_R']) > 0:
                    indicators['WILLIAMS_R_value'] = indicators['WILLIAMS_R'][-1]
                if 'ROC' in indicators:
                    roc_array = indicators['ROC']
                    indicators['ROC_value'] = roc_array[-1] if len(roc_array) > 0 else 0
                if 'OBV' in indicators and len(indicators['OBV']) > 0:
                    indicators['OBV_value'] = indicators['OBV'][-1]
                if 'OBV_MA20' in indicators and len(indicators['OBV_MA20']) > 0:
                    indicators['OBV_MA20_value'] = indicators['OBV_MA20'][-1]
                if 'ATR' in indicators and len(indicators['ATR']) > 0:
                    indicators['ATR_value'] = indicators['ATR'][-1]
                
                # 볼린저 밴드 계산
                if 'BB_UPPER' in indicators and 'BB_LOWER' in indicators:
                    bb_upper = indicators['BB_UPPER'][-1] if len(indicators['BB_UPPER']) > 0 else 0
                    bb_middle = indicators['BB_MIDDLE'][-1] if len(indicators.get('BB_MIDDLE', [])) > 0 else 0
                    bb_lower = indicators['BB_LOWER'][-1] if len(indicators['BB_LOWER']) > 0 else 0
                    
                    indicators['BB_UPPER_value'] = bb_upper
                    indicators['BB_MIDDLE_value'] = bb_middle
                    indicators['BB_LOWER_value'] = bb_lower
                    
                    # 볼린저 밴드 포지션 계산
                    if bb_upper > 0 and bb_lower > 0:
                        bb_range = bb_upper - bb_lower
                        if bb_range > 0 and len(close) > 0:
                            indicators['BB_POSITION'] = (close[-1] - bb_lower) / bb_range
                        else:
                            indicators['BB_POSITION'] = 0.5
                    else:
                        indicators['BB_POSITION'] = 0.5
                    
                    # 볼린저 밴드 대역폭
                    if bb_middle > 0:
                        indicators['BB_BANDWIDTH'] = (bb_upper - bb_lower) / bb_middle
                    else:
                        indicators['BB_BANDWIDTH'] = 0
                
                # VWAP 계산
                if len(close) >= 1 and len(volume) >= 1:
                    typical_price = (high + low + close) / 3
                    vwap = np.sum(typical_price * volume) / np.sum(volume) if np.sum(volume) > 0 else 0
                    indicators['VWAP'] = vwap
                
                # 가격 정보 (전략에서 사용)
                indicators['close'] = close[-1] if len(close) > 0 else 0
                indicators['high'] = high[-1] if len(high) > 0 else 0
                indicators['low'] = low[-1] if len(low) > 0 else 0
                
                return indicators
            
            # ========== 2단계: 캐시된 지표가 부족하면 재계산 (타입 안전 처리) ==========
            logging.debug(f"⚠️ 캐시된 지표 부족 ({cached_indicators_found}개), 재계산 수행")
            
            # 기본 가격 데이터 추출 및 타입 변환 (안전하게)
            try:
                close = np.array(chart_data['close'].values, dtype=np.float64)
                high = np.array(chart_data['high'].values, dtype=np.float64)
                low = np.array(chart_data['low'].values, dtype=np.float64)
                volume = np.array(chart_data['volume'].values, dtype=np.float64)
            except Exception as type_error:
                logging.error(f"가격 데이터 타입 변환 실패: {type_error}")
                # 타입 변환 실패 시 강제 변환 시도
                close = pd.to_numeric(chart_data['close'], errors='coerce').fillna(0).values.astype(np.float64)
                high = pd.to_numeric(chart_data['high'], errors='coerce').fillna(0).values.astype(np.float64)
                low = pd.to_numeric(chart_data['low'], errors='coerce').fillna(0).values.astype(np.float64)
                volume = pd.to_numeric(chart_data['volume'], errors='coerce').fillna(0).values.astype(np.float64)
            
            # 이동평균선
            if len(close) >= 5:
                indicators['MA5'] = talib.SMA(close, timeperiod=5)
                indicators['MA5_value'] = indicators['MA5'][-1] if len(indicators['MA5']) > 0 else 0
            
            if len(close) >= 10:
                indicators['MA10'] = talib.SMA(close, timeperiod=10)
                indicators['MA10_value'] = indicators['MA10'][-1] if len(indicators['MA10']) > 0 else 0
            
            if len(close) >= 20:
                indicators['MA20'] = talib.SMA(close, timeperiod=20)
                indicators['MA20_value'] = indicators['MA20'][-1] if len(indicators['MA20']) > 0 else 0
            
            if len(close) >= 60:
                indicators['MA60'] = talib.SMA(close, timeperiod=60)
                indicators['MA60_value'] = indicators['MA60'][-1] if len(indicators['MA60']) > 0 else 0
            
            # RSI
            if len(close) >= 14:
                indicators['RSI'] = talib.RSI(close, timeperiod=14)
                indicators['RSI_value'] = indicators['RSI'][-1] if len(indicators['RSI']) > 0 else 50
                
                # RSI 신호선 (RSI의 이동평균)
                if len(indicators['RSI']) >= 5:
                    rsi_signal = talib.SMA(indicators['RSI'], timeperiod=5)
                    indicators['RSI_SIGNAL_value'] = rsi_signal[-1] if len(rsi_signal) > 0 else 50
                else:
                    indicators['RSI_SIGNAL_value'] = 50
            
            # MACD
            if len(close) >= 26:
                macd, macd_signal, macd_hist = talib.MACD(close)
                indicators['MACD'] = macd
                indicators['MACD_SIGNAL'] = macd_signal
                indicators['MACD_HIST'] = macd_hist
                indicators['MACD_value'] = macd[-1] if len(macd) > 0 else 0
                indicators['MACD_SIGNAL_value'] = macd_signal[-1] if len(macd_signal) > 0 else 0
                
                # MACD_HIST (Oscillator) - MACD 히스토그램
                indicators['MACD_HIST_value'] = macd_hist[-1] if len(macd_hist) > 0 else 0
            
            # 스토캐스틱
            if len(high) >= 14 and len(low) >= 14:
                stoch_k, stoch_d = talib.STOCH(high, low, close)
                indicators['STOCHK_value'] = stoch_k[-1] if len(stoch_k) > 0 else 50
                indicators['STOCHD_value'] = stoch_d[-1] if len(stoch_d) > 0 else 50
            
            # 볼린저 밴드
            if len(close) >= 20:
                bb_upper, bb_middle, bb_lower = talib.BBANDS(close, timeperiod=20)
                indicators['BB_UPPER_value'] = bb_upper[-1] if len(bb_upper) > 0 else 0
                indicators['BB_MIDDLE_value'] = bb_middle[-1] if len(bb_middle) > 0 else 0
                indicators['BB_LOWER_value'] = bb_lower[-1] if len(bb_lower) > 0 else 0
                
                # 볼린저 밴드 포지션 계산
                if indicators['BB_UPPER_value'] > 0 and indicators['BB_LOWER_value'] > 0:
                    bb_range = indicators['BB_UPPER_value'] - indicators['BB_LOWER_value']
                    if bb_range > 0:
                        indicators['BB_POSITION'] = (close[-1] - indicators['BB_LOWER_value']) / bb_range
                    else:
                        indicators['BB_POSITION'] = 0.5
                else:
                    indicators['BB_POSITION'] = 0.5
                
                # 볼린저 밴드 대역폭
                if indicators['BB_MIDDLE_value'] > 0:
                    indicators['BB_BANDWIDTH'] = bb_range / indicators['BB_MIDDLE_value']
                else:
                    indicators['BB_BANDWIDTH'] = 0
            
            # ATR (Average True Range)
            if len(high) >= 14 and len(low) >= 14:
                atr = talib.ATR(high, low, close, timeperiod=14)
                indicators['ATR_value'] = atr[-1] if len(atr) > 0 else 0
            
            # Williams %R
            if len(high) >= 14 and len(low) >= 14:
                williams_r = talib.WILLR(high, low, close, timeperiod=14)
                indicators['WILLIAMS_R_value'] = williams_r[-1] if len(williams_r) > 0 else -50
            
            # ROC (Rate of Change)
            if len(close) >= 12:
                roc = talib.ROC(close, timeperiod=12)
                indicators['ROC'] = roc  # 전체 배열 저장 (ROC_recent 계산용)
                indicators['ROC_value'] = roc[-1] if len(roc) > 0 else 0
            
            # OBV (On Balance Volume)
            if len(close) >= 1 and len(volume) >= 1:
                obv = talib.OBV(close, volume)
                indicators['OBV_value'] = obv[-1] if len(obv) > 0 else 0
                
                # OBV 이동평균
                if len(obv) >= 20:
                    obv_ma20 = talib.SMA(obv, timeperiod=20)
                    indicators['OBV_MA20_value'] = obv_ma20[-1] if len(obv_ma20) > 0 else 0
            
            # VWAP 계산
            if len(close) >= 1 and len(volume) >= 1:
                typical_price = (high + low + close) / 3
                total_volume = np.sum(volume)
                vwap = np.sum(typical_price * volume) / total_volume if total_volume > 0 else 0
                indicators['VWAP'] = vwap
            
            # 가격 정보
            indicators['close'] = close[-1] if len(close) > 0 else 0
            indicators['high'] = high[-1] if len(high) > 0 else 0
            indicators['low'] = low[-1] if len(low) > 0 else 0
            
            return indicators
            
        except Exception as ex:
            import traceback
            logging.error(f"차트 지표 추출 실패: {ex}")
            logging.error(f"에러 상세:\n{traceback.format_exc()}")
            return {}
    
    @staticmethod
    def calculate_additional_indicators(indicators, chart_data):
        """추가 지표 계산 (실제 전략에서 사용되는 지표만)"""
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
            
            return additional
            
        except Exception as ex:
            logging.error(f"추가 지표 계산 실패: {ex}")
            return {}

# ==================== 백테스팅용 로컬 변수 빌더 ====================
def build_backtest_buy_locals(code, chart_data, portfolio_info=None):
    """백테스팅용 매수 로컬 변수 생성"""
    try:
        if chart_data.empty:
            return {}
        
        # 기본 지표 추출
        indicators = KiwoomIndicatorExtractor.extract_chart_indicators(chart_data)
        additional = KiwoomIndicatorExtractor.calculate_additional_indicators(indicators, chart_data)
        
        # 로컬 변수 딕셔너리 생성
        locals_dict = {}
        locals_dict.update(indicators)
        locals_dict.update(additional)
        
        # 포트폴리오 정보 추가
        if portfolio_info:
            locals_dict.update(portfolio_info)
        
        # 백테스팅 특화 변수들
        locals_dict['code'] = code
        locals_dict['chart_data'] = chart_data
        locals_dict['current_time'] = datetime.now()
        
        # 거래량 관련 변수
        if not chart_data.empty:
            volume_series = chart_data['volume']
            if len(volume_series) > 0:
                locals_dict['avg_volume'] = volume_series.mean()
                locals_dict['volume_ratio'] = volume_series.iloc[-1] / locals_dict['avg_volume'] if locals_dict['avg_volume'] > 0 else 1
        
        return locals_dict
        
    except Exception as ex:
        logging.error(f"백테스팅 매수 로컬 변수 생성 실패 ({code}): {ex}")
        return {}

def build_backtest_sell_locals(code, chart_data, buy_price, buy_time, current_price, portfolio_info=None):
    """백테스팅용 매도 로컬 변수 생성"""
    try:
        if chart_data.empty:
            return {}
        
        # 기본 지표 추출
        indicators = KiwoomIndicatorExtractor.extract_chart_indicators(chart_data)
        additional = KiwoomIndicatorExtractor.calculate_additional_indicators(indicators, chart_data)
        
        # 로컬 변수 딕셔너리 생성
        locals_dict = {}
        locals_dict.update(indicators)
        locals_dict.update(additional)
        
        # 매매 관련 변수
        locals_dict['code'] = code
        locals_dict['buy_price'] = buy_price
        locals_dict['buy_time'] = buy_time
        locals_dict['current_price'] = current_price
        
        # 수익률 계산
        if buy_price > 0:
            locals_dict['current_profit_pct'] = (current_price - buy_price) / buy_price * 100
        else:
            locals_dict['current_profit_pct'] = 0
        
        # 보유 시간 계산
        if buy_time:
            hold_time = datetime.now() - buy_time
            locals_dict['hold_minutes'] = hold_time.total_seconds() / 60
            locals_dict['hold_hours'] = hold_time.total_seconds() / 3600
        else:
            locals_dict['hold_minutes'] = 0
            locals_dict['hold_hours'] = 0
        
        # 포트폴리오 정보 추가
        if portfolio_info:
            locals_dict.update(portfolio_info)
            
            # 최고가 추적
            highest_price = portfolio_info.get('highest_prices', {}).get(code, current_price)
            locals_dict['highest_price'] = highest_price
            
            # 최고점 대비 하락률
            if highest_price > 0:
                locals_dict['from_peak_pct'] = (current_price - highest_price) / highest_price * 100
            else:
                locals_dict['from_peak_pct'] = 0
        
        # 시간 관련 변수
        current_hour = datetime.now().hour
        locals_dict['after_market_close'] = current_hour >= 15  # 15시 이후 (장 마감 후)
        locals_dict['market_open'] = 9 <= current_hour <= 15  # 장 개장 시간
        
        return locals_dict
        
    except Exception as ex:
        logging.error(f"백테스팅 매도 로컬 변수 생성 실패 ({code}): {ex}")
        return {}

# ==================== 실시간 트레이딩용 로컬 변수 빌더 ====================
def build_realtime_buy_locals(code, kiwoom_data, chart_data, portfolio_info=None):
    """실시간 매수 로컬 변수 생성"""
    try:
        # 실시간 데이터 지표 추출
        realtime_indicators = KiwoomIndicatorExtractor.extract_realtime_indicators(kiwoom_data)
        
        # 틱봉 차트 데이터 지표 추출 (30틱)
        chart_indicators = {}
        if not chart_data.empty:
            chart_indicators = KiwoomIndicatorExtractor.extract_chart_indicators(chart_data)
        
        # 3분봉 데이터 지표 추출 (min_data)
        min_chart_indicators = {}
        min_additional_indicators = {}
        min_data = kiwoom_data.get('min_data', {})
        if min_data and isinstance(min_data, dict):
            # 1) min_data에 이미 있는 지표를 우선 직매핑 (최신값)
            try:
                def _last_scalar(arr):
                    try:
                        if isinstance(arr, (list, np.ndarray)) and len(arr) > 0:
                            return float(arr[-1])
                    except Exception:
                        return None
                    return None
                direct_map = {
                    'MA5': 'MA5_value',
                    'MA10': 'MA10_value',
                    'MA20': 'MA20_value',
                    'RSI': 'RSI_value',
                    'MACD': 'MACD_value',
                    'MACD_SIGNAL': 'MACD_SIGNAL_value',
                    'MACD_HIST': 'MACD_HIST_value',
                    'STOCH_K': 'STOCHK_value',
                    'STOCH_D': 'STOCHD_value',
                    'WILLIAMS_R': 'WILLIAMS_R_value',
                    'ROC': 'ROC_value',
                    'OBV': 'OBV_value',
                    'OBV_MA20': 'OBV_MA20_value',
                    'ATR': 'ATR_value',
                }
                for src, dst in direct_map.items():
                    if src in min_data and dst not in min_chart_indicators:
                        val = _last_scalar(min_data.get(src))
                        if val is not None and not np.isnan(val):
                            min_chart_indicators[dst] = val
                # BBANDS 관련: 최근 값으로 POSITION/BANDWIDTH 계산 시도
                bb_u = _last_scalar(min_data.get('BB_UPPER')) if 'BB_UPPER' in min_data else None
                bb_m = _last_scalar(min_data.get('BB_MIDDLE')) if 'BB_MIDDLE' in min_data else None
                bb_l = _last_scalar(min_data.get('BB_LOWER')) if 'BB_LOWER' in min_data else None
                if bb_u is not None:
                    min_chart_indicators['BB_UPPER_value'] = bb_u
                if bb_m is not None:
                    min_chart_indicators['BB_MIDDLE_value'] = bb_m
                if bb_l is not None:
                    min_chart_indicators['BB_LOWER_value'] = bb_l
                # POSITION/BANDWIDTH 계산 (가능할 때)
                if all(v is not None for v in [bb_u, bb_m, bb_l]) and 'close' in min_data and min_data.get('close'):
                    try:
                        last_close = _last_scalar(min_data.get('close'))
                        if last_close is not None and bb_u > bb_l:
                            min_chart_indicators['BB_POSITION'] = (last_close - bb_l) / (bb_u - bb_l)
                        if bb_m and bb_m != 0:
                            min_chart_indicators['BB_BANDWIDTH'] = (bb_u - bb_l) / bb_m
                    except Exception:
                        pass
                # VWAP: 스칼라가 캐시에 있을 수 있음
                if 'VWAP' in min_data and isinstance(min_data.get('VWAP'), (int, float)):
                    min_chart_indicators['VWAP'] = float(min_data.get('VWAP'))
            except Exception:
                pass

            # 2) 부족한 지표만 재계산하여 보충
            if min_data.get('close'):
                min_chart_df = pd.DataFrame({
                    'close': min_data.get('close', []),
                    'high': min_data.get('high', []),
                    'low': min_data.get('low', []),
                    'open': min_data.get('open', []),
                    'volume': min_data.get('volume', [])
                })
                if not min_chart_df.empty:
                    calc_inds = KiwoomIndicatorExtractor.extract_chart_indicators(min_chart_df)
                    for k, v in calc_inds.items():
                        if k not in min_chart_indicators:
                            min_chart_indicators[k] = v
                    # 3분봉 추가 지표 계산
                    min_additional_indicators = KiwoomIndicatorExtractor.calculate_additional_indicators(min_chart_indicators, min_chart_df)
        
        # 로컬 변수 딕셔너리 생성
        locals_dict = {}
        locals_dict.update(realtime_indicators)
        
        # 30틱 차트 지표를 tic_ 접두사로 추가
        tic_keys = ['MA5_value', 'MA20_value', 'MA60_value', 'RSI_value', 'RSI_SIGNAL_value',
                    'MACD_value', 'MACD_SIGNAL_value', 'MACD_HIST_value', 'STOCHK_value', 'STOCHD_value',
                    'WILLIAMS_R_value', 'ROC_value', 'OBV_value', 'OBV_MA20_value', 'ATR_value',
                    'BB_UPPER_value', 'BB_MIDDLE_value', 'BB_LOWER_value', 'BB_POSITION', 'BB_BANDWIDTH',
                    'VWAP', 'close', 'high', 'low']
        
        for key in tic_keys:
            if key in chart_indicators:
                # _value 접미사 제거하고 tic_ 접두사 추가
                if key.endswith('_value'):
                    new_key = f'tic_{key[:-6]}'  # _value 제거
                else:
                    new_key = f'tic_{key}'
                locals_dict[new_key] = chart_indicators[key]
        
        # 캐시 틱 데이터의 VWAP을 우선 사용 (스칼라 값)
        try:
            tick_cache = kiwoom_data.get('tick_data', {}) if isinstance(kiwoom_data, dict) else {}
            if isinstance(tick_cache, dict):
                cache_vwap = tick_cache.get('VWAP', None)
                if cache_vwap is not None and not isinstance(cache_vwap, (list, np.ndarray)):
                    locals_dict['tic_VWAP'] = cache_vwap
        except Exception:
            pass
        
        # 캐시에 없고 지표 계산에서 존재하면 그 값을 사용
        if 'tic_VWAP' not in locals_dict and 'VWAP' in chart_indicators:
            locals_dict['tic_VWAP'] = chart_indicators['VWAP']
        
        # 최종 안전장치: 여전히 없으면 0으로 설정하여 NameError 방지
        if 'tic_VWAP' not in locals_dict:
            locals_dict['tic_VWAP'] = 0

        # 캐시 틱 지표 배열에서 최신값으로 보강 (MA/RSI/MACD 등)
        try:
            if isinstance(tick_cache, dict):
                def _last_scalar(arr):
                    try:
                        if isinstance(arr, (list, np.ndarray)) and len(arr) > 0:
                            return float(arr[-1])
                    except Exception:
                        return None
                    return None
                cache_map = {
                    'MA5': 'tic_MA5',
                    'MA20': 'tic_MA20',
                    'MA60': 'tic_MA60',
                    'MA120': 'tic_MA120',
                    'RSI': 'tic_RSI',
                    'MACD': 'tic_MACD',
                    'MACD_SIGNAL': 'tic_MACD_SIGNAL',
                    'MACD_HIST': 'tic_MACD_HIST',
                }
                for src_key, dst_key in cache_map.items():
                    if dst_key not in locals_dict and src_key in tick_cache:
                        val = _last_scalar(tick_cache.get(src_key))
                        if val is not None and not np.isnan(val):
                            locals_dict[dst_key] = val
        except Exception:
            pass

        # 차트 지표 배열에서 보강 (캐시에 없을 때), 최종 기본값 0
        if 'tic_MACD_HIST' not in locals_dict:
            if 'MACD_HIST' in chart_indicators:
                try:
                    arr = chart_indicators['MACD_HIST']
                    if isinstance(arr, (list, np.ndarray)) and len(arr) > 0:
                        locals_dict['tic_MACD_HIST'] = float(arr[-1])
                except Exception:
                    pass
        if 'tic_MACD_HIST' not in locals_dict:
            locals_dict['tic_MACD_HIST'] = 0
        
        # ROC 배열도 추가 (ROC_recent 계산용)
        if 'ROC' in chart_indicators:
            locals_dict['ROC'] = chart_indicators['ROC']
        
        # 3분봉 지표를 min3_ 접두사로 추가
        min3_keys = ['MA5_value', 'MA10_value', 'MA20_value', 'RSI_value', 'RSI_SIGNAL_value',
                     'MACD_value', 'MACD_SIGNAL_value', 'MACD_HIST_value', 'STOCHK_value', 'STOCHD_value',
                     'WILLIAMS_R_value', 'ROC_value', 'OBV_value', 'OBV_MA20_value', 'ATR_value',
                     'BB_UPPER_value', 'BB_MIDDLE_value', 'BB_LOWER_value', 'BB_POSITION', 'BB_BANDWIDTH',
                     'VWAP', 'close', 'high', 'low']
        
        for key in min3_keys:
            if key in min_chart_indicators:
                # _value 접미사 제거하고 min3_ 접두사 추가
                if key.endswith('_value'):
                    new_key = f'min3_{key[:-6]}'  # _value 제거
                else:
                    new_key = f'min3_{key}'
                locals_dict[new_key] = min_chart_indicators[key]
            else:
                # MA10이 없으면 기본값 0 설정 (에러 방지)
                if key == 'MA10_value':
                    locals_dict['min3_MA10'] = 0
        
        # 3분봉 추가 지표 (ROC_recent 등)
        for key, value in min_additional_indicators.items():
            if not isinstance(value, (list, np.ndarray)):
                locals_dict[f'min3_{key}'] = value
        
        # 키움 API 특화 변수들
        locals_dict['code'] = code
        locals_dict['kiwoom_data'] = kiwoom_data
        locals_dict['chart_data'] = chart_data
        
        # 기본 가격 변수
        current_price = kiwoom_data.get('current_price', 0)
        locals_dict['current_price'] = current_price
        # C는 데이터 캐시의 틱 차트 종가를 우선 사용, 없을 때만 실시간 현재가 사용
        try:
            tick_close_scalar = None
            if 'tic_close' in locals_dict and isinstance(locals_dict['tic_close'], (int, float)):
                tick_close_scalar = float(locals_dict['tic_close'])
            elif not chart_data.empty and 'close' in chart_data.columns and len(chart_data['close']) > 0:
                tick_close_scalar = float(chart_data['close'].iloc[-1])
            locals_dict['C'] = tick_close_scalar if tick_close_scalar is not None else current_price
        except Exception:
            locals_dict['C'] = current_price
        
        # 체결강도 계산 (키움 API에서 제공하는 경우)
        strength = kiwoom_data.get('strength', 0)
        locals_dict['strength'] = strength
        
        # 거래량
        volume = kiwoom_data.get('volume', 0)
        locals_dict['volume'] = volume
        
        # 최근 가격 변수들
        if not chart_data.empty:
            recent_prices = chart_data['close'].tail(30).tolist()
            locals_dict['tic_C_recent'] = recent_prices
            
            # 전체 틱 종가 리스트
            all_close_prices = chart_data['close'].tolist()
            locals_dict['tic_close_list'] = all_close_prices
        
        # 갭 관련 변수 (전일 종가 대비)
        previous_close = kiwoom_data.get('previous_close', 0)
        if previous_close > 0 and current_price > 0:
            gap_rate = (current_price - previous_close) / previous_close * 100
            locals_dict['gap_hold'] = gap_rate > 2  # 2% 이상 갭상승 시 True
            locals_dict['gap_rate'] = gap_rate
        else:
            locals_dict['gap_hold'] = False
            locals_dict['gap_rate'] = 0
        
        # 변동성 돌파 변수
        if 'tic_ATR' in locals_dict and current_price > 0:
            atr = locals_dict['tic_ATR']
            locals_dict['volatility_breakout'] = atr > current_price * 0.01  # ATR이 현재가의 1% 이상
        else:
            locals_dict['volatility_breakout'] = False
        
        # Volume Profile 관련 변수 계산 (거래량 가중 분석)
        if not chart_data.empty and len(chart_data) > 0:
            # VWAP (Volume Weighted Average Price) 계산
            typical_price = (chart_data['high'] + chart_data['low'] + chart_data['close']) / 3
            total_volume = chart_data['volume'].sum()
            
            if total_volume > 0:
                vwap = (typical_price * chart_data['volume']).sum() / total_volume
                locals_dict['VP_POC'] = vwap  # VWAP을 POC로 근사
                
                # VP_POSITION: 현재가가 VWAP 대비 어느 위치인지
                # VWAP을 중심(0.5)으로 ±1 표준편차 범위를 0~1로 매핑
                price_std = chart_data['close'].std()
                if price_std > 0:
                    # (현재가 - VWAP) / 표준편차를 -1~1 범위로 정규화 후 0~1로 변환
                    normalized = (current_price - vwap) / price_std
                    # -2σ ~ +2σ 범위를 0~1로 매핑 (대부분의 데이터가 이 범위 내)
                    locals_dict['VP_POSITION'] = max(0, min(1, (normalized + 2) / 4))
                else:
                    locals_dict['VP_POSITION'] = 0.5
            else:
                locals_dict['VP_POC'] = current_price
                locals_dict['VP_POSITION'] = 0.5
            
            # 볼륨 프로파일 돌파 (거래량 급증)
            avg_volume = chart_data['volume'].mean()
            volume_ratio = volume / avg_volume if avg_volume > 0 else 1
            locals_dict['volume_profile_breakout'] = volume_ratio > 2  # 평균 거래량의 2배 이상
        else:
            locals_dict['VP_POC'] = current_price
            locals_dict['VP_POSITION'] = 0.5
            locals_dict['volume_profile_breakout'] = False
        
        # 포지티브 캔들 확인
        open_price = kiwoom_data.get('open', 0)
        locals_dict['positive_candle'] = current_price > open_price if open_price > 0 else False
        
        # 포트폴리오 정보 추가
        if portfolio_info:
            locals_dict.update(portfolio_info)
        
        return locals_dict
        
    except Exception as ex:
        logging.error(f"실시간 매수 로컬 변수 생성 실패 ({code}): {ex}")
        return {}

def build_realtime_sell_locals(code, kiwoom_data, chart_data, buy_price, buy_time, portfolio_info=None):
    """실시간 매도 로컬 변수 생성"""
    try:
        # 실시간 데이터 지표 추출
        realtime_indicators = KiwoomIndicatorExtractor.extract_realtime_indicators(kiwoom_data)
        
        # 틱봉 차트 데이터 지표 추출 (30틱)
        chart_indicators = {}
        if not chart_data.empty:
            chart_indicators = KiwoomIndicatorExtractor.extract_chart_indicators(chart_data)
        
        # 3분봉 데이터 지표 추출 (min_data)
        min_chart_indicators = {}
        min_additional_indicators = {}
        min_data = kiwoom_data.get('min_data', {})
        if min_data and isinstance(min_data, dict):
            # 1) min_data에 이미 있는 지표를 우선 직매핑 (최신값)
            try:
                def _last_scalar(arr):
                    try:
                        if isinstance(arr, (list, np.ndarray)) and len(arr) > 0:
                            return float(arr[-1])
                    except Exception:
                        return None
                    return None
                direct_map = {
                    'MA5': 'MA5_value',
                    'MA10': 'MA10_value',
                    'MA20': 'MA20_value',
                    'RSI': 'RSI_value',
                    'MACD': 'MACD_value',
                    'MACD_SIGNAL': 'MACD_SIGNAL_value',
                    'MACD_HIST': 'MACD_HIST_value',
                    'STOCH_K': 'STOCHK_value',
                    'STOCH_D': 'STOCHD_value',
                    'WILLIAMS_R': 'WILLIAMS_R_value',
                    'ROC': 'ROC_value',
                    'OBV': 'OBV_value',
                    'OBV_MA20': 'OBV_MA20_value',
                    'ATR': 'ATR_value',
                }
                for src, dst in direct_map.items():
                    if src in min_data and dst not in min_chart_indicators:
                        val = _last_scalar(min_data.get(src))
                        if val is not None and not np.isnan(val):
                            min_chart_indicators[dst] = val
                # BBANDS 관련
                bb_u = _last_scalar(min_data.get('BB_UPPER')) if 'BB_UPPER' in min_data else None
                bb_m = _last_scalar(min_data.get('BB_MIDDLE')) if 'BB_MIDDLE' in min_data else None
                bb_l = _last_scalar(min_data.get('BB_LOWER')) if 'BB_LOWER' in min_data else None
                if bb_u is not None:
                    min_chart_indicators['BB_UPPER_value'] = bb_u
                if bb_m is not None:
                    min_chart_indicators['BB_MIDDLE_value'] = bb_m
                if bb_l is not None:
                    min_chart_indicators['BB_LOWER_value'] = bb_l
                if all(v is not None for v in [bb_u, bb_m, bb_l]) and 'close' in min_data and min_data.get('close'):
                    try:
                        last_close = _last_scalar(min_data.get('close'))
                        if last_close is not None and bb_u > bb_l:
                            min_chart_indicators['BB_POSITION'] = (last_close - bb_l) / (bb_u - bb_l)
                        if bb_m and bb_m != 0:
                            min_chart_indicators['BB_BANDWIDTH'] = (bb_u - bb_l) / bb_m
                    except Exception:
                        pass
                if 'VWAP' in min_data and isinstance(min_data.get('VWAP'), (int, float)):
                    min_chart_indicators['VWAP'] = float(min_data.get('VWAP'))
            except Exception:
                pass

            # 2) 부족한 지표만 재계산하여 보충
            if min_data.get('close'):
                min_chart_df = pd.DataFrame({
                    'close': min_data.get('close', []),
                    'high': min_data.get('high', []),
                    'low': min_data.get('low', []),
                    'open': min_data.get('open', []),
                    'volume': min_data.get('volume', [])
                })
                if not min_chart_df.empty:
                    calc_inds = KiwoomIndicatorExtractor.extract_chart_indicators(min_chart_df)
                    for k, v in calc_inds.items():
                        if k not in min_chart_indicators:
                            min_chart_indicators[k] = v
                    # 3분봉 추가 지표 계산
                    min_additional_indicators = KiwoomIndicatorExtractor.calculate_additional_indicators(min_chart_indicators, min_chart_df)
        
        # 로컬 변수 딕셔너리 생성
        locals_dict = {}
        locals_dict.update(realtime_indicators)
        
        # 30틱 차트 지표를 tic_ 접두사로 추가
        tic_keys = ['MA5_value', 'MA20_value', 'MA60_value', 'RSI_value', 'RSI_SIGNAL_value',
                    'MACD_value', 'MACD_SIGNAL_value', 'MACD_HIST_value', 'STOCHK_value', 'STOCHD_value',
                    'WILLIAMS_R_value', 'ROC_value', 'OBV_value', 'OBV_MA20_value', 'ATR_value',
                    'BB_UPPER_value', 'BB_MIDDLE_value', 'BB_LOWER_value', 'BB_POSITION', 'BB_BANDWIDTH',
                    'VWAP', 'close', 'high', 'low']
        
        for key in tic_keys:
            if key in chart_indicators:
                # _value 접미사 제거하고 tic_ 접두사 추가
                if key.endswith('_value'):
                    new_key = f'tic_{key[:-6]}'  # _value 제거
                else:
                    new_key = f'tic_{key}'
                locals_dict[new_key] = chart_indicators[key]
        
        # 캐시 틱 데이터의 VWAP을 우선 사용 (스칼라 값)
        try:
            tick_cache = kiwoom_data.get('tick_data', {}) if isinstance(kiwoom_data, dict) else {}
            if isinstance(tick_cache, dict):
                cache_vwap = tick_cache.get('VWAP', None)
                if cache_vwap is not None and not isinstance(cache_vwap, (list, np.ndarray)):
                    locals_dict['tic_VWAP'] = cache_vwap
        except Exception:
            pass
        
        # 캐시에 없고 지표 계산에서 존재하면 그 값을 사용
        if 'tic_VWAP' not in locals_dict and 'VWAP' in chart_indicators:
            locals_dict['tic_VWAP'] = chart_indicators['VWAP']
        
        # 최종 안전장치: 여전히 없으면 0으로 설정하여 NameError 방지
        if 'tic_VWAP' not in locals_dict:
            locals_dict['tic_VWAP'] = 0

        # 캐시 틱 지표 배열에서 최신값으로 보강 (MA/RSI/MACD 등)
        try:
            if isinstance(tick_cache, dict):
                def _last_scalar(arr):
                    try:
                        if isinstance(arr, (list, np.ndarray)) and len(arr) > 0:
                            return float(arr[-1])
                    except Exception:
                        return None
                    return None
                cache_map = {
                    'MA5': 'tic_MA5',
                    'MA20': 'tic_MA20',
                    'MA60': 'tic_MA60',
                    'MA120': 'tic_MA120',
                    'RSI': 'tic_RSI',
                    'MACD': 'tic_MACD',
                    'MACD_SIGNAL': 'tic_MACD_SIGNAL',
                    'MACD_HIST': 'tic_MACD_HIST',
                }
                for src_key, dst_key in cache_map.items():
                    if dst_key not in locals_dict and src_key in tick_cache:
                        val = _last_scalar(tick_cache.get(src_key))
                        if val is not None and not np.isnan(val):
                            locals_dict[dst_key] = val
        except Exception:
            pass

        # 차트 지표 배열에서 보강 (캐시에 없을 때), 최종 기본값 0
        if 'tic_MACD_HIST' not in locals_dict:
            if 'MACD_HIST' in chart_indicators:
                try:
                    arr = chart_indicators['MACD_HIST']
                    if isinstance(arr, (list, np.ndarray)) and len(arr) > 0:
                        locals_dict['tic_MACD_HIST'] = float(arr[-1])
                except Exception:
                    pass
        if 'tic_MACD_HIST' not in locals_dict:
            locals_dict['tic_MACD_HIST'] = 0
        
        # ROC 배열도 추가 (ROC_recent 계산용)
        if 'ROC' in chart_indicators:
            locals_dict['ROC'] = chart_indicators['ROC']
        
        # 3분봉 지표를 min3_ 접두사로 추가
        min3_keys = ['MA5_value', 'MA10_value', 'MA20_value', 'RSI_value', 'RSI_SIGNAL_value',
                     'MACD_value', 'MACD_SIGNAL_value', 'MACD_HIST_value', 'STOCHK_value', 'STOCHD_value',
                     'WILLIAMS_R_value', 'ROC_value', 'OBV_value', 'OBV_MA20_value', 'ATR_value',
                     'BB_UPPER_value', 'BB_MIDDLE_value', 'BB_LOWER_value', 'BB_POSITION', 'BB_BANDWIDTH',
                     'VWAP', 'close', 'high', 'low']
        
        for key in min3_keys:
            if key in min_chart_indicators:
                # _value 접미사 제거하고 min3_ 접두사 추가
                if key.endswith('_value'):
                    new_key = f'min3_{key[:-6]}'  # _value 제거
                else:
                    new_key = f'min3_{key}'
                locals_dict[new_key] = min_chart_indicators[key]
            else:
                # MA10이 없으면 기본값 0 설정 (에러 방지)
                if key == 'MA10_value':
                    locals_dict['min3_MA10'] = 0
        
        # 3분봉 추가 지표 (ROC_recent 등)
        for key, value in min_additional_indicators.items():
            if not isinstance(value, (list, np.ndarray)):
                locals_dict[f'min3_{key}'] = value
        
        # 매매 관련 변수
        current_price = kiwoom_data.get('current_price', 0)
        locals_dict['code'] = code
        locals_dict['buy_price'] = buy_price
        locals_dict['buy_time'] = buy_time
        locals_dict['current_price'] = current_price
        # C는 데이터 캐시의 틱 차트 종가를 우선 사용, 없을 때만 실시간 현재가 사용
        try:
            tick_close_scalar = None
            if 'tic_close' in locals_dict and isinstance(locals_dict['tic_close'], (int, float)):
                tick_close_scalar = float(locals_dict['tic_close'])
            elif not chart_data.empty and 'close' in chart_data.columns and len(chart_data['close']) > 0:
                tick_close_scalar = float(chart_data['close'].iloc[-1])
            locals_dict['C'] = tick_close_scalar if tick_close_scalar is not None else current_price
        except Exception:
            locals_dict['C'] = current_price
        
        # 체결강도 및 거래량
        locals_dict['strength'] = kiwoom_data.get('strength', 0)
        locals_dict['volume'] = kiwoom_data.get('volume', 0)
        
        # 수익률 계산
        if buy_price > 0 and current_price > 0:
            locals_dict['current_profit_pct'] = (current_price - buy_price) / buy_price * 100
        else:
            locals_dict['current_profit_pct'] = 0
        
        # 보유 시간 계산
        if buy_time:
            hold_time = datetime.now() - buy_time
            locals_dict['hold_minutes'] = hold_time.total_seconds() / 60
            locals_dict['hold_hours'] = hold_time.total_seconds() / 3600
        else:
            locals_dict['hold_minutes'] = 0
            locals_dict['hold_hours'] = 0
        
        # 포트폴리오 정보 추가
        if portfolio_info:
            locals_dict.update(portfolio_info)
            
            # 최고가 추적
            highest_price = portfolio_info.get('highest_prices', {}).get(code, current_price)
            locals_dict['highest_price'] = highest_price
            
            # 최고점 대비 하락률
            if highest_price > 0 and current_price > 0:
                locals_dict['from_peak_pct'] = (current_price - highest_price) / highest_price * 100
            else:
                locals_dict['from_peak_pct'] = 0
        
        # 시간 관련 변수
        current_hour = datetime.now().hour
        locals_dict['after_market_close'] = current_hour >= 15  # 15시 이후 (장 마감 후)
        locals_dict['market_open'] = 9 <= current_hour <= 15  # 장 개장 시간
        
        # 갭 관련 변수
        previous_close = kiwoom_data.get('previous_close', 0)
        if previous_close > 0 and current_price > 0:
            gap_rate = (current_price - previous_close) / previous_close * 100
            locals_dict['gap_hold'] = gap_rate > 2
            locals_dict['gap_rate'] = gap_rate
        else:
            locals_dict['gap_hold'] = False
            locals_dict['gap_rate'] = 0
        
        # 최근 가격 변수들 (매도 전략용)
        if not chart_data.empty:
            recent_prices = chart_data['close'].tail(30).tolist()
            locals_dict['tic_C_recent'] = recent_prices
            
            # 전체 틱 종가 리스트
            all_close_prices = chart_data['close'].tolist()
            locals_dict['tic_close_list'] = all_close_prices
        
        # 변동성 돌파 및 Volume Profile 변수 (매도 전략에도 필요)
        if 'tic_ATR' in locals_dict and current_price > 0:
            atr = locals_dict['tic_ATR']
            locals_dict['volatility_breakout'] = atr > current_price * 0.01
        else:
            locals_dict['volatility_breakout'] = False
        
        if not chart_data.empty and len(chart_data) > 0:
            avg_volume = chart_data['volume'].mean()
            volume = locals_dict['volume']
            volume_ratio = volume / avg_volume if avg_volume > 0 else 1
            locals_dict['volume_profile_breakout'] = volume_ratio > 2
        else:
            locals_dict['volume_profile_breakout'] = False
        
        return locals_dict
        
    except Exception as ex:
        logging.error(f"실시간 매도 로컬 변수 생성 실패 ({code}): {ex}")
        return {}

# ==================== 전략 평가 헬퍼 함수 ====================
def evaluate_buy_strategies(code, strategies, kiwoom_data, chart_data, portfolio_info=None):
    """매수 전략 평가"""
    try:
        # 로컬 변수 생성
        safe_locals = build_realtime_buy_locals(code, kiwoom_data, chart_data, portfolio_info)
        
        # 전략 평가
        return evaluate_strategies(strategies, safe_locals, code, "매수")
        
    except Exception as ex:
        logging.error(f"매수 전략 평가 실패 ({code}): {ex}")
        return False, None

def evaluate_sell_strategies(code, strategies, kiwoom_data, chart_data, buy_price, buy_time, portfolio_info=None):
    """매도 전략 평가"""
    try:
        # 로컬 변수 생성
        safe_locals = build_realtime_sell_locals(code, kiwoom_data, chart_data, buy_price, buy_time, portfolio_info)
        
        # 전략 평가
        return evaluate_strategies(strategies, safe_locals, code, "매도")
        
    except Exception as ex:
        logging.error(f"매도 전략 평가 실패 ({code}): {ex}")
        return False, None

# ==================== 설정 파일에서 전략 로드 ====================
def load_strategies_from_config(config_file='settings.ini'):
    """설정 파일에서 전략 로드"""
    try:
        import configparser
        config = configparser.RawConfigParser()
        config.read(config_file, encoding='utf-8')
        
        strategies = {}
        
        # 전략 섹션들 처리
        strategy_sections = ['VI 발동', '급등주', '갭상승', '통합 전략']
        
        for section in strategy_sections:
            if config.has_section(section):
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
                            logging.warning(f"매수 전략 파싱 실패: {section}.{option}")
                    
                    elif option.startswith('sell_stg_'):
                        try:
                            strategy_data = json.loads(config.get(section, option))
                            strategies[section]['sell_strategies'].append(strategy_data)
                        except json.JSONDecodeError:
                            logging.warning(f"매도 전략 파싱 실패: {section}.{option}")
        
        return strategies
        
    except Exception as ex:
        logging.error(f"전략 로드 실패: {ex}")
        return {}

# ==================== 전략 실행 헬퍼 ====================
def execute_strategy_signal(code, signal_type, strategy, kiwoom_data, chart_data, portfolio_info=None, buy_price=0, buy_time=None):
    """전략 신호 실행"""
    try:
        if signal_type == "buy":
            return evaluate_buy_strategies(code, [strategy], kiwoom_data, chart_data, portfolio_info)
        elif signal_type == "sell":
            return evaluate_sell_strategies(code, [strategy], kiwoom_data, chart_data, buy_price, buy_time, portfolio_info)
        else:
            logging.error(f"알 수 없는 신호 타입: {signal_type}")
            return False, None
            
    except Exception as ex:
        logging.error(f"전략 신호 실행 실패 ({code}, {signal_type}): {ex}")
        return False, None
