"""
키움 REST API 기반 전략 평가 및 지표 처리 유틸리티 모듈
크레온 플러스 API를 키움 REST API로 전면 리팩토링
"""
import json
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import talib
from typing import Dict, List, Optional, Any

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
    for strategy in strategies:
        try:
            condition = strategy.get('content', '')
            if not condition:
                continue
                
            if eval(condition, STRATEGY_SAFE_GLOBALS, safe_locals):
                strategy_name = strategy.get('name', '전략')
                if code:
                    logging.debug(f"{code}: {strategy_name} 조건 충족")
                return True, strategy
                
        except Exception as ex:
            strategy_name = strategy.get('name', '알 수 없는 전략')
            logging.error(f"{code} {strategy_type} 전략 '{strategy_name}' 평가 오류: {ex}")
    
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
        """차트 데이터에서 기술적 지표 추출"""
        try:
            if chart_data.empty:
                return {}
            
            indicators = {}
            
            # 기본 가격 데이터
            close = chart_data['close'].values
            high = chart_data['high'].values
            low = chart_data['low'].values
            volume = chart_data['volume'].values
            
            # 이동평균선
            if len(close) >= 5:
                indicators['MA5'] = talib.SMA(close, timeperiod=5)
                indicators['MAT5'] = indicators['MA5'][-1] if len(indicators['MA5']) > 0 else 0
            
            if len(close) >= 20:
                indicators['MA20'] = talib.SMA(close, timeperiod=20)
                indicators['MAT20'] = indicators['MA20'][-1] if len(indicators['MA20']) > 0 else 0
            
            if len(close) >= 60:
                indicators['MA60'] = talib.SMA(close, timeperiod=60)
                indicators['MAT60'] = indicators['MA60'][-1] if len(indicators['MA60']) > 0 else 0
            
            # RSI
            if len(close) >= 14:
                indicators['RSI'] = talib.RSI(close, timeperiod=14)
                indicators['RSIT'] = indicators['RSI'][-1] if len(indicators['RSI']) > 0 else 50
                
                # RSI 신호선 (RSI의 이동평균)
                if len(indicators['RSI']) >= 5:
                    rsi_signal = talib.SMA(indicators['RSI'], timeperiod=5)
                    indicators['RSIT_SIGNAL'] = rsi_signal[-1] if len(rsi_signal) > 0 else 50
                else:
                    indicators['RSIT_SIGNAL'] = 50
            
            # MACD
            if len(close) >= 26:
                macd, macd_signal, macd_hist = talib.MACD(close)
                indicators['MACD'] = macd
                indicators['MACD_SIGNAL'] = macd_signal
                indicators['MACD_HIST'] = macd_hist
                indicators['MACDT'] = macd[-1] if len(macd) > 0 else 0
                indicators['MACDT_SIGNAL'] = macd_signal[-1] if len(macd_signal) > 0 else 0
                
                # OSC (Oscillator) - MACD 히스토그램
                indicators['OSC'] = macd_hist[-1] if len(macd_hist) > 0 else 0
                indicators['OSCT'] = macd_hist[-1] if len(macd_hist) > 0 else 0
            
            # 스토캐스틱
            if len(high) >= 14 and len(low) >= 14:
                stoch_k, stoch_d = talib.STOCH(high, low, close)
                indicators['STOCHK'] = stoch_k[-1] if len(stoch_k) > 0 else 50
                indicators['STOCHD'] = stoch_d[-1] if len(stoch_d) > 0 else 50
            
            # 볼린저 밴드
            if len(close) >= 20:
                bb_upper, bb_middle, bb_lower = talib.BBANDS(close, timeperiod=20)
                indicators['BB_UPPER'] = bb_upper[-1] if len(bb_upper) > 0 else 0
                indicators['BB_MIDDLE'] = bb_middle[-1] if len(bb_middle) > 0 else 0
                indicators['BB_LOWER'] = bb_lower[-1] if len(bb_lower) > 0 else 0
                
                # 볼린저 밴드 포지션 계산
                if indicators['BB_UPPER'] > 0 and indicators['BB_LOWER'] > 0:
                    bb_range = indicators['BB_UPPER'] - indicators['BB_LOWER']
                    if bb_range > 0:
                        indicators['BB_POSITION'] = (close[-1] - indicators['BB_LOWER']) / bb_range
                    else:
                        indicators['BB_POSITION'] = 0.5
                else:
                    indicators['BB_POSITION'] = 0.5
                
                # 볼린저 밴드 대역폭
                if indicators['BB_MIDDLE'] > 0:
                    indicators['BB_BANDWIDTH'] = bb_range / indicators['BB_MIDDLE']
                else:
                    indicators['BB_BANDWIDTH'] = 0
            
            # ATR (Average True Range)
            if len(high) >= 14 and len(low) >= 14:
                atr = talib.ATR(high, low, close, timeperiod=14)
                indicators['ATR'] = atr[-1] if len(atr) > 0 else 0
            
            # Williams %R
            if len(high) >= 14 and len(low) >= 14:
                williams_r = talib.WILLR(high, low, close, timeperiod=14)
                indicators['WILLIAMS_R'] = williams_r[-1] if len(williams_r) > 0 else -50
            
            # ROC (Rate of Change)
            if len(close) >= 12:
                roc = talib.ROC(close, timeperiod=12)
                indicators['ROC'] = roc[-1] if len(roc) > 0 else 0
            
            # OBV (On Balance Volume)
            if len(close) >= 1 and len(volume) >= 1:
                obv = talib.OBV(close, volume)
                indicators['OBV'] = obv[-1] if len(obv) > 0 else 0
                
                # OBV 이동평균
                if len(obv) >= 20:
                    obv_ma20 = talib.SMA(obv, timeperiod=20)
                    indicators['OBV_MA20'] = obv_ma20[-1] if len(obv_ma20) > 0 else 0
            
            # VWAP 계산 (키움 API에서 제공하지 않는 경우)
            if len(close) >= 1 and len(volume) >= 1:
                typical_price = (high + low + close) / 3
                vwap = np.sum(typical_price * volume) / np.sum(volume)
                indicators['VWAP'] = vwap
            
            # 틱봉 가격 정보 (전략에서 사용)
            indicators['tick_close_price'] = close[-1] if len(close) > 0 else 0
            indicators['tick_high_price'] = high[-1] if len(high) > 0 else 0
            indicators['tick_low_price'] = low[-1] if len(low) > 0 else 0
            
            # 분봉 가격 정보 (전략에서 사용)
            indicators['min_close_price'] = close[-1] if len(close) > 0 else 0
            indicators['min_high_price'] = high[-1] if len(high) > 0 else 0
            indicators['min_low_price'] = low[-1] if len(low) > 0 else 0
            
            # 분봉 이동평균 (전략에서 사용)
            if len(close) >= 5:
                indicators['MAM5'] = talib.SMA(close, timeperiod=5)[-1]
            if len(close) >= 10:
                indicators['MAM10'] = talib.SMA(close, timeperiod=10)[-1]
            if len(close) >= 20:
                indicators['MAM20'] = talib.SMA(close, timeperiod=20)[-1]
            
            return indicators
            
        except Exception as ex:
            logging.error(f"차트 지표 추출 실패: {ex}")
            return {}
    
    @staticmethod
    def calculate_additional_indicators(indicators, chart_data):
        """추가 지표 계산"""
        try:
            additional = {}
            
            # 최근 가격 리스트 (최근 30틱)
            if not chart_data.empty and len(chart_data) > 0:
                close_prices = chart_data['close'].tail(30).tolist()
                additional['tick_C_recent'] = close_prices
                additional['min_close'] = min(close_prices) if close_prices else 0
                additional['max_close'] = max(close_prices) if close_prices else 0
            
            # 최근 RSI 리스트
            if 'RSI' in indicators and len(indicators['RSI']) > 0:
                rsi_recent = indicators['RSI'][-30:].tolist()
                additional['RSI_recent'] = rsi_recent
                additional['min_RSI'] = min(rsi_recent) if rsi_recent else 50
                additional['max_RSI'] = max(rsi_recent) if rsi_recent else 50
            
            # 최근 스토캐스틱 리스트
            if 'STOCHK' in indicators:
                stochk_recent = [indicators.get('STOCHK', 50)]  # 단일 값이므로 리스트로 변환
                additional['STOCHK_recent'] = stochk_recent
                additional['min_STOCHK'] = min(stochk_recent) if stochk_recent else 50
                additional['max_STOCHK'] = max(stochk_recent) if stochk_recent else 50
            
            if 'STOCHD' in indicators:
                stochd_recent = [indicators.get('STOCHD', 50)]
                additional['STOCHD_recent'] = stochd_recent
                additional['min_STOCHD'] = min(stochd_recent) if stochd_recent else 50
                additional['max_STOCHD'] = max(stochd_recent) if stochd_recent else 50
            
            # 최근 Williams %R 리스트
            if 'WILLIAMS_R' in indicators:
                williams_recent = [indicators.get('WILLIAMS_R', -50)]
                additional['WILLIAMS_R_recent'] = williams_recent
                additional['min_WILLIAMS_R'] = min(williams_recent) if williams_recent else -50
                additional['max_WILLIAMS_R'] = max(williams_recent) if williams_recent else -50
            
            # 최근 ROC 리스트
            if 'ROC' in indicators:
                roc_recent = [indicators.get('ROC', 0)]
                additional['ROC_recent'] = roc_recent
                additional['min_ROC'] = min(roc_recent) if roc_recent else 0
                additional['max_ROC'] = max(roc_recent) if roc_recent else 0
            
            # 최근 Williams %R 리스트
            if 'WILLIAMS_R' in indicators:
                williams_recent = [indicators.get('WILLIAMS_R', -50)]
                additional['WILLIAMS_R_recent'] = williams_recent
                additional['min_WILLIAMS_R'] = min(williams_recent) if williams_recent else -50
                additional['max_WILLIAMS_R'] = max(williams_recent) if williams_recent else -50
            
            # 최근 OSC 리스트
            if 'OSC' in indicators:
                osc_recent = [indicators.get('OSC', 0)]
                additional['OSC_recent'] = osc_recent
                additional['min_OSC'] = min(osc_recent) if osc_recent else 0
                additional['max_OSC'] = max(osc_recent) if osc_recent else 0
            
            # 분봉 최소값들 (전략에서 사용)
            if not chart_data.empty and len(chart_data) > 0:
                close_prices = chart_data['close'].tail(30).tolist()
                additional['min_close'] = min(close_prices) if close_prices else 0
            
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
        
        # 차트 데이터 지표 추출
        chart_indicators = {}
        additional_indicators = {}
        if not chart_data.empty:
            chart_indicators = KiwoomIndicatorExtractor.extract_chart_indicators(chart_data)
            additional_indicators = KiwoomIndicatorExtractor.calculate_additional_indicators(chart_indicators, chart_data)
        
        # 로컬 변수 딕셔너리 생성
        locals_dict = {}
        locals_dict.update(realtime_indicators)
        locals_dict.update(chart_indicators)
        locals_dict.update(additional_indicators)
        
        # 키움 API 특화 변수들
        locals_dict['code'] = code
        locals_dict['kiwoom_data'] = kiwoom_data
        locals_dict['chart_data'] = chart_data
        
        # 체결강도 계산 (키움 API에서 제공하는 경우)
        strength = kiwoom_data.get('strength', 0)
        locals_dict['strength'] = strength
        
        # VWAP 관련 변수
        current_price = kiwoom_data.get('current_price', 0)
        vwap = chart_indicators.get('VWAP', 0)
        locals_dict['tick_VWAP'] = vwap
        
        # 최근 가격 변수들
        if not chart_data.empty:
            recent_prices = chart_data['close'].tail(30).tolist()
            locals_dict['tick_C_recent'] = recent_prices
            locals_dict['min_close'] = min(recent_prices) if recent_prices else current_price
            locals_dict['min_VWAP'] = vwap  # VWAP 최소값은 현재 VWAP과 동일
        
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
        if 'ATR' in chart_indicators and current_price > 0:
            atr = chart_indicators['ATR']
            locals_dict['volatility_breakout'] = atr > current_price * 0.01  # ATR이 현재가의 1% 이상
        else:
            locals_dict['volatility_breakout'] = False
        
        # Volume Profile 관련 변수 (간단한 구현)
        volume = kiwoom_data.get('volume', 0)
        locals_dict['VP_POC'] = current_price  # Price of Control (간단히 현재가로 설정)
        locals_dict['VP_POSITION'] = 0.5  # 기본값
        
        # 볼륨 프로파일 돌파 (거래량 급증)
        if not chart_data.empty and len(chart_data) > 0:
            avg_volume = chart_data['volume'].mean()
            volume_ratio = volume / avg_volume if avg_volume > 0 else 1
            locals_dict['volume_profile_breakout'] = volume_ratio > 2  # 평균 거래량의 2배 이상
        else:
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
        
        # 차트 데이터 지표 추출
        chart_indicators = {}
        additional_indicators = {}
        if not chart_data.empty:
            chart_indicators = KiwoomIndicatorExtractor.extract_chart_indicators(chart_data)
            additional_indicators = KiwoomIndicatorExtractor.calculate_additional_indicators(chart_indicators, chart_data)
        
        # 로컬 변수 딕셔너리 생성
        locals_dict = {}
        locals_dict.update(realtime_indicators)
        locals_dict.update(chart_indicators)
        locals_dict.update(additional_indicators)
        
        # 매매 관련 변수
        current_price = kiwoom_data.get('current_price', 0)
        locals_dict['code'] = code
        locals_dict['buy_price'] = buy_price
        locals_dict['buy_time'] = buy_time
        locals_dict['current_price'] = current_price
        
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
