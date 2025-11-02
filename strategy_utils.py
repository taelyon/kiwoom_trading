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
    logger = logging.getLogger(__name__)

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
                logger.debug(f"🔍 [{code}] 전략 평가 중: {strategy_name}")
                logger.debug(f"🔍 [{code}] 조건: {condition}")
                logger.debug(f"🔍 [{code}] 현재 수익률: {current_profit:.2f}%")
                
            result = eval(condition, STRATEGY_SAFE_GLOBALS, safe_locals)
            
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
                KiwoomIndicatorExtractor.logger.debug(f"✅ 캐시된 지표 {cached_indicators_found}개 활용 (재계산 생략)")
                
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
            KiwoomIndicatorExtractor.logger.debug(f"⚠️ 캐시된 지표 부족 ({cached_indicators_found}개), 재계산 수행")
            
            # 기본 가격 데이터 추출 및 타입 변환 (안전하게)
            try:
                close = np.array(chart_data['close'].values, dtype=np.float64)
                high = np.array(chart_data['high'].values, dtype=np.float64)
                low = np.array(chart_data['low'].values, dtype=np.float64)
                volume = np.array(chart_data['volume'].values, dtype=np.float64)
            except Exception as type_error:
                KiwoomIndicatorExtractor.logger.error(f"가격 데이터 타입 변환 실패: {type_error}", exc_info=True)
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
            
            return additional
            
        except Exception as ex:
            logger.error(f"추가 지표 계산 실패: {ex}", exc_info=True)
            return {}

# ==================== 백테스팅용 로컬 변수 빌더 ====================
def build_backtest_buy_locals(code, chart_data, portfolio_info=None):
    """백테스팅용 매수 로컬 변수 생성"""
    logger = logging.getLogger(__name__)
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
        
        # 백테스팅에서 실시간 전략과 호환성을 위해 접두사(tic_, min3_)가 붙은 변수 생성
        # chart_data (DataFrame)의 컬럼명을 기반으로 변수 생성
        if not chart_data.empty:
            last_row = chart_data.iloc[-1]
            for col_name in chart_data.columns:
                if col_name.startswith('tic_') or col_name.startswith('min3_'):
                    # 예: 'tic_ma5' -> 'tic_MA5'
                    try:
                        parts = col_name.split('_', 1)
                        var_name = parts[0] + '_' + parts[1].upper()
                        locals_dict[var_name] = last_row[col_name]
                    except IndexError:
                        locals_dict[col_name] = last_row[col_name]
        
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
        logger.error(f"백테스팅 매수 로컬 변수 생성 실패 ({code}): {ex}", exc_info=True)
        return {}

def build_backtest_sell_locals(code, chart_data, buy_price, buy_time, current_price, portfolio_info=None):
    """백테스팅용 매도 로컬 변수 생성"""
    logger = logging.getLogger(__name__)
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
        
        # 백테스팅에서 실시간 전략과 호환성을 위해 접두사(tic_, min3_)가 붙은 변수 생성
        # chart_data (DataFrame)의 컬럼명을 기반으로 변수 생성
        if not chart_data.empty:
            last_row = chart_data.iloc[-1]
            for col_name in chart_data.columns:
                if col_name.startswith('tic_') or col_name.startswith('min3_'):
                    # 예: 'tic_ma5' -> 'tic_MA5'
                    try:
                        parts = col_name.split('_', 1)
                        var_name = parts[0] + '_' + parts[1].upper()
                        locals_dict[var_name] = last_row[col_name]
                    except IndexError:
                        locals_dict[col_name] = last_row[col_name]
        
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
        logger.error(f"백테스팅 매도 로컬 변수 생성 실패 ({code}): {ex}", exc_info=True)
        return {}

# ==================== 설정 파일에서 전략 로드 ====================
def load_strategies_from_config(config_file='settings.ini'):
    """설정 파일에서 전략 로드"""
    logger = logging.getLogger(__name__)
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
                            logger.warning(f"매수 전략 파싱 실패: {section}.{option}")
                    
                    elif option.startswith('sell_stg_'):
                        try:
                            strategy_data = json.loads(config.get(section, option))
                            strategies[section]['sell_strategies'].append(strategy_data)
                        except json.JSONDecodeError:
                            logger.warning(f"매도 전략 파싱 실패: {section}.{option}")
        
        return strategies
        
    except Exception as ex:
        logger.error(f"전략 로드 실패: {ex}", exc_info=True)
        return {}
