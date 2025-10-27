"""
키움 REST API 기반 백테스팅 엔진
크레온 플러스 API를 키움 REST API로 전면 리팩토링
"""
# 표준 라이브러리
import configparser
import copy
import json
import logging
import sqlite3
from datetime import datetime, timedelta

# 서드파티 라이브러리
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.figure import Figure

# 로컬 모듈
from strategy_utils import (
    KiwoomIndicatorExtractor,
    STRATEGY_SAFE_GLOBALS,
    build_backtest_buy_locals,
    build_backtest_sell_locals,
    evaluate_strategies
)

plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False

class KiwoomBacktester:
    """키움 REST API 기반 백테스팅 엔진"""
    
    def __init__(self, db_path, config_file='settings.ini', initial_cash=10000000):
        self.db_path = db_path
        self.config_file = config_file
        self.initial_cash = initial_cash
        
        # settings.ini 로드
        self.config = configparser.RawConfigParser()
        if config_file:
            try:
                self.config.read(config_file, encoding='utf-8')
                logging.info(f"✅ 설정 파일 로드: {config_file}")
            except Exception as ex:
                logging.error(f"설정 파일 로드 실패: {ex}")
        
        # 포트폴리오
        self.cash = initial_cash
        self.holdings = {}
        self.buy_prices = {}
        self.buy_times = {}
        self.highest_prices = {}
        
        # 매매 설정
        self.max_holdings = 3
        self.position_size = 0.3
        
        # 거래 기록
        self.trades = []
        self.equity_curve = []
        
        # 전략 로드
        self.strategies = self.load_strategies()
        
        # 백테스팅 결과
        self.results = {}
        
        logging.info(f"키움 백테스터 초기화 완료 (초기 자금: {initial_cash:,}원)")
    
    def load_strategies(self):
        """전략 로드"""
        try:
            strategies = {}
            strategy_sections = ['VI 발동', '급등주', '갭상승', '통합 전략']
            
            for section in strategy_sections:
                if self.config.has_section(section):
                    strategies[section] = {
                        'buy_strategies': [],
                        'sell_strategies': []
                    }
                    
                    # 섹션의 모든 옵션 확인
                    for option in self.config.options(section):
                        if option.startswith('buy_stg_'):
                            try:
                                strategy_data = json.loads(self.config.get(section, option))
                                strategies[section]['buy_strategies'].append(strategy_data)
                            except json.JSONDecodeError:
                                logging.warning(f"매수 전략 파싱 실패: {section}.{option}")
                        
                        elif option.startswith('sell_stg_'):
                            try:
                                strategy_data = json.loads(self.config.get(section, option))
                                strategies[section]['sell_strategies'].append(strategy_data)
                            except json.JSONDecodeError:
                                logging.warning(f"매도 전략 파싱 실패: {section}.{option}")
            
            logging.info(f"전략 로드 완료: {list(strategies.keys())}")
            return strategies
            
        except Exception as ex:
            logging.error(f"전략 로드 실패: {ex}")
            return {}
    
    def load_stock_data(self, code, start_date, end_date, data_type='auto'):
        """통합 주식 데이터 로드 (stock_data 테이블 사용)
        
        Args:
            code: 종목코드
            start_date: 시작일
            end_date: 종료일
            data_type: 'auto', 'tick', 'minute'
        """
        try:
            conn = sqlite3.connect(self.db_path)
            
            if data_type == 'auto':
                # 통합 데이터 로드 (틱봉 기준)
                df = self._load_integrated_data(conn, code, start_date, end_date)
            elif data_type == 'tick':
                # 틱봉 데이터만 로드 (통합 테이블에서)
                df = self._load_tick_from_integrated(conn, code, start_date, end_date)
            elif data_type == 'minute':
                # 분봉 데이터만 로드 (통합 테이블에서)
                df = self._load_minute_from_integrated(conn, code, start_date, end_date)
            else:
                logging.error(f"지원하지 않는 데이터 타입: {data_type}")
                return pd.DataFrame()
            
            conn.close()
            
            if df.empty:
                logging.warning(f"데이터 없음: {code} ({start_date} ~ {end_date})")
                return pd.DataFrame()
            
            # datetime 컬럼을 인덱스로 설정
            df['datetime'] = pd.to_datetime(df['datetime'])
            df.set_index('datetime', inplace=True)
            
            # 결측값 처리
            df = df.ffill().fillna(0)
            
            logging.info(f"데이터 로드 완료: {code} ({len(df)}개 레코드, 타입: {data_type})")
            return df
            
        except Exception as ex:
            logging.error(f"데이터 로드 실패 ({code}): {ex}")
            return pd.DataFrame()
    
    def _load_integrated_data(self, conn, code, start_date, end_date):
        """통합 데이터 로드 (틱봉 기준, 분봉 지표 포함)"""
        try:
            # 먼저 테이블의 실제 컬럼을 확인
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(stock_data)")
            columns_info = cursor.fetchall()
            available_columns = [col[1] for col in columns_info]
            
            # 기본 컬럼들
            base_columns = ['datetime', 'open', 'high', 'low', 'close', 'volume']
            
            # 틱봉 데이터 컬럼들
            tick_columns = ['tick_open', 'tick_high', 'tick_low', 'tick_close', 'tick_volume', 'tick_strength']
            
            # 기술적 지표 컬럼들 (실제 존재하는 것만)
            indicator_columns = []
            for col in available_columns:
                if col.startswith('tick_') and col not in tick_columns and col != 'tick_last_tic_cnt':
                    indicator_columns.append(col)
                elif col.startswith('min_') and col != 'min_last_tic_cnt':
                    indicator_columns.append(col)
            
            # 모든 컬럼 결합
            all_columns = base_columns + tick_columns + indicator_columns
            existing_columns = [col for col in all_columns if col in available_columns]
            
            # SQL 쿼리 생성
            columns_str = ', '.join(existing_columns)
            query = f'''
                SELECT {columns_str}
                FROM stock_data 
                WHERE code = ? AND datetime BETWEEN ? AND ?
                ORDER BY datetime
            '''
            
            df = pd.read_sql_query(query, conn, params=(code, start_date, end_date))
            
            if not df.empty:
                logging.info(f"통합 데이터 로드 완료: {code} ({len(df)}개 레코드, {len(existing_columns)}개 컬럼)")
                # 컬럼명 정리 (백워드 호환성)
                df = self._standardize_column_names(df)
            
            return df
            
        except Exception as ex:
            logging.error(f"통합 데이터 로드 실패 ({code}): {ex}")
            return pd.DataFrame()
    
    def _load_tick_from_integrated(self, conn, code, start_date, end_date):
        """통합 테이블에서 틱봉 데이터만 로드"""
        try:
            query = '''
                SELECT 
                    datetime,
                    tick_open as open, tick_high as high, tick_low as low, 
                    tick_close as close, tick_volume as volume, tick_strength as strength,
                    tick_ma5 as ma5, tick_ma10 as ma10, tick_ma20 as ma20, tick_ma50 as ma50,
                    tick_ema5 as ema5, tick_ema10 as ema10, tick_ema20 as ema20,
                    tick_rsi as rsi, tick_macd as macd, tick_macd_signal as macd_signal, tick_macd_hist as macd_hist
                FROM stock_data 
                WHERE code = ? AND datetime BETWEEN ? AND ?
                ORDER BY datetime
            '''
            
            df = pd.read_sql_query(query, conn, params=(code, start_date, end_date))
            
            if not df.empty:
                logging.info(f"틱봉 데이터 로드 완료: {code} ({len(df)}개 레코드)")
            
            return df
            
        except Exception as ex:
            logging.error(f"틱봉 데이터 로드 실패 ({code}): {ex}")
            return pd.DataFrame()
    
    def _load_minute_from_integrated(self, conn, code, start_date, end_date):
        """통합 테이블에서 분봉 데이터만 로드 (중복 제거)"""
        try:
            query = '''
                SELECT 
                    datetime,
                    open, high, low, close, volume,
                    min_ma5 as ma5, min_ma10 as ma10, min_ma20 as ma20, min_ma50 as ma50,
                    min_ema5 as ema5, min_ema10 as ema10, min_ema20 as ema20,
                    min_rsi as rsi, min_macd as macd, min_macd_signal as macd_signal, min_macd_hist as macd_hist
                FROM stock_data 
                WHERE code = ? AND datetime BETWEEN ? AND ?
                AND min_ma5 IS NOT NULL  -- 분봉 지표가 있는 데이터만
                ORDER BY datetime
            '''
            
            df = pd.read_sql_query(query, conn, params=(code, start_date, end_date))
            
            if not df.empty:
                logging.info(f"분봉 데이터 로드 완료: {code} ({len(df)}개 레코드)")
            
            return df
            
        except Exception as ex:
            logging.error(f"분봉 데이터 로드 실패 ({code}): {ex}")
            return pd.DataFrame()
    
    def _standardize_column_names(self, df):
        """컬럼명을 표준화하여 기존 백테스팅 코드와 호환"""
        try:
            # 기본 OHLCV는 그대로 사용
            # 기술적 지표는 접두사 제거하여 표준화
            column_mapping = {
                'tick_ma5': 'ma5', 'tick_ma10': 'ma10', 'tick_ma20': 'ma20', 'tick_ma50': 'ma50',
                'tick_ema5': 'ema5', 'tick_ema10': 'ema10', 'tick_ema20': 'ema20',
                'tick_rsi': 'rsi', 'tick_macd': 'macd', 'tick_macd_signal': 'macd_signal', 'tick_macd_hist': 'macd_hist',
                'tick_strength': 'strength'
            }
            
            df = df.rename(columns=column_mapping)
            return df
            
        except Exception as ex:
            logging.error(f"컬럼명 표준화 실패: {ex}")
            return df
    
    def _load_tick_data(self, conn, code, start_date, end_date):
        """틱 데이터 로드"""
        try:
            query = """
                SELECT timestamp as datetime, open, high, low, close, volume 
                FROM tick_data 
                WHERE code = ? AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp
            """
            return pd.read_sql_query(query, conn, params=(code, start_date, end_date))
        except Exception as ex:
            logging.error(f"틱 데이터 로드 실패 ({code}): {ex}")
            return pd.DataFrame()
    
    def _load_minute_data(self, conn, code, start_date, end_date):
        """분봉 데이터 로드"""
        try:
            query = """
                SELECT timestamp as datetime, open, high, low, close, volume 
                FROM minute_data 
                WHERE code = ? AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp
            """
            return pd.read_sql_query(query, conn, params=(code, start_date, end_date))
        except Exception as ex:
            logging.error(f"분봉 데이터 로드 실패 ({code}): {ex}")
            return pd.DataFrame()
    
    def simulate_kiwoom_data(self, row, code):
        """키움 REST API 데이터 형식으로 시뮬레이션"""
        try:
            # 기본 가격 정보
            kiwoom_data = {
                'code': code,
                'current_price': row['close'],
                'open': row['open'],
                'high': row['high'],
                'low': row['low'],
                'volume': row['volume'],
                'previous_close': row['close'],  # 백테스팅에서는 현재가와 동일하게 설정
                'change': 0,
                'change_rate': 0,
                'turnover': row['volume'] * row['close'],
                'market_cap': 0,  # 시가총액은 별도 계산 필요
                'per': 0,
                'pbr': 0,
                'strength': np.random.randint(100, 200),  # 체결강도 시뮬레이션
                'bid_price': row['close'] * 0.999,
                'ask_price': row['close'] * 1.001,
                'bid_volume': row['volume'] * 0.3,
                'ask_volume': row['volume'] * 0.7
            }
            
            return kiwoom_data
            
        except Exception as ex:
            logging.error(f"키움 데이터 시뮬레이션 실패: {ex}")
            return {}
    
    def can_buy(self):
        """매수 가능 여부 확인"""
        return len(self.holdings) < self.max_holdings and self.cash > 100000
    
    def can_sell(self, code):
        """매도 가능 여부 확인"""
        return code in self.holdings
    
    def calculate_position_size(self, price):
        """포지션 크기 계산"""
        try:
            available_cash = self.cash * self.position_size
            shares = int(available_cash / price)
            return max(0, shares)
        except Exception as ex:
            logging.error(f"포지션 크기 계산 실패: {ex}")
            return 0
    
    def execute_buy(self, code, price, strategy_name, timestamp):
        """매수 실행"""
        try:
            if not self.can_buy():
                return False
            
            shares = self.calculate_position_size(price)
            if shares == 0:
                return False
            
            total_cost = shares * price
            if total_cost > self.cash:
                return False
            
            # 매수 실행
            self.cash -= total_cost
            self.holdings[code] = shares
            self.buy_prices[code] = price
            self.buy_times[code] = timestamp
            self.highest_prices[code] = price
            
            # 거래 기록
            trade = {
                'timestamp': timestamp,
                'code': code,
                'action': 'buy',
                'shares': shares,
                'price': price,
                'amount': total_cost,
                'strategy': strategy_name,
                'cash_remaining': self.cash,
                'holdings_count': len(self.holdings)
            }
            self.trades.append(trade)
            
            logging.info(f"매수 실행: {code} {shares}주 @ {price} ({strategy_name})")
            return True
            
        except Exception as ex:
            logging.error(f"매수 실행 실패 ({code}): {ex}")
            return False
    
    def execute_sell(self, code, price, strategy_name, timestamp):
        """매도 실행"""
        try:
            if not self.can_sell(code):
                return False
            
            shares = self.holdings[code]
            total_amount = shares * price
            
            # 매도 실행
            self.cash += total_amount
            
            # 수익률 계산
            buy_price = self.buy_prices[code]
            profit_loss = (price - buy_price) * shares
            profit_rate = (price - buy_price) / buy_price * 100
            
            # 거래 기록
            trade = {
                'timestamp': timestamp,
                'code': code,
                'action': 'sell',
                'shares': shares,
                'price': price,
                'amount': total_amount,
                'strategy': strategy_name,
                'buy_price': buy_price,
                'profit_loss': profit_loss,
                'profit_rate': profit_rate,
                'cash_remaining': self.cash,
                'holdings_count': len(self.holdings) - 1
            }
            self.trades.append(trade)
            
            # 포트폴리오에서 제거
            del self.holdings[code]
            del self.buy_prices[code]
            del self.buy_times[code]
            del self.highest_prices[code]
            
            logging.info(f"매도 실행: {code} {shares}주 @ {price} (수익률: {profit_rate:.2f}%) ({strategy_name})")
            return True
            
        except Exception as ex:
            logging.error(f"매도 실행 실패 ({code}): {ex}")
            return False
    
    def update_portfolio_value(self, timestamp, current_prices):
        """포트폴리오 가치 업데이트"""
        try:
            total_value = self.cash
            
            for code, shares in self.holdings.items():
                if code in current_prices:
                    total_value += shares * current_prices[code]
                    
                    # 최고가 업데이트
                    if code in self.highest_prices:
                        self.highest_prices[code] = max(self.highest_prices[code], current_prices[code])
            
            # 자산 곡선 기록
            equity_point = {
                'timestamp': timestamp,
                'total_value': total_value,
                'cash': self.cash,
                'holdings_value': total_value - self.cash,
                'holdings_count': len(self.holdings)
            }
            self.equity_curve.append(equity_point)
            
        except Exception as ex:
            logging.error(f"포트폴리오 가치 업데이트 실패: {ex}")
    
    def run_backtest(self, codes, start_date, end_date, strategy_name='통합 전략', data_type='auto'):
        """백테스팅 실행
        
        Args:
            codes: 종목코드 리스트
            start_date: 시작일
            end_date: 종료일
            strategy_name: 전략명
            data_type: 데이터 타입 ('auto', 'tick', 'minute')
        """
        try:
            logging.info(f"백테스팅 시작: {strategy_name} ({start_date} ~ {end_date}, 데이터타입: {data_type})")
            
            if strategy_name not in self.strategies:
                logging.error(f"전략을 찾을 수 없음: {strategy_name}")
                return False
            
            # 데이터 로드 (틱 데이터 우선)
            all_data = {}
            for code in codes:
                data = self.load_stock_data(code, start_date, end_date, data_type)
                if not data.empty:
                    all_data[code] = data
            
            if not all_data:
                logging.error("백테스팅할 데이터가 없습니다.")
                return False
            
            # 공통 타임스탬프 생성
            all_timestamps = set()
            for data in all_data.values():
                all_timestamps.update(data.index)
            
            timestamps = sorted(all_timestamps)
            logging.info(f"백테스팅 기간: {len(timestamps)}개 시점")
            
            # 전략 설정
            buy_strategies = self.strategies[strategy_name]['buy_strategies']
            sell_strategies = self.strategies[strategy_name]['sell_strategies']
            
            # 백테스팅 실행
            for i, timestamp in enumerate(timestamps):
                try:
                    current_prices = {}
                    
                    # 현재 시점의 가격 정보 수집
                    for code, data in all_data.items():
                        if timestamp in data.index:
                            current_prices[code] = data.loc[timestamp, 'close']
                    
                    # 매도 신호 확인 (보유 종목)
                    for code in list(self.holdings.keys()):
                        if code in all_data and timestamp in all_data[code].index:
                            # 현재 시점까지의 데이터 슬라이스
                            chart_data = all_data[code].loc[:timestamp]
                            
                            if len(chart_data) > 20:  # 최소 데이터 요구사항
                                # 키움 데이터 시뮬레이션
                                kiwoom_data = self.simulate_kiwoom_data(
                                    all_data[code].loc[timestamp], code
                                )
                                
                                # 매도 전략 평가
                                portfolio_info = {
                                    'holdings': self.holdings.copy(),
                                    'buy_prices': self.buy_prices.copy(),
                                    'buy_times': self.buy_times.copy(),
                                    'highest_prices': self.highest_prices.copy()
                                }
                                
                                success, strategy = evaluate_strategies(
                                    sell_strategies,
                                    build_backtest_sell_locals(
                                        code, chart_data, 
                                        self.buy_prices[code], 
                                        self.buy_times[code],
                                        current_prices[code],
                                        portfolio_info
                                    ),
                                    code, "매도"
                                )
                                
                                if success:
                                    self.execute_sell(code, current_prices[code], strategy['name'], timestamp)
                    
                    # 매수 신호 확인
                    for code, data in all_data.items():
                        if (code not in self.holdings and 
                            timestamp in data.index and 
                            len(data.loc[:timestamp]) > 20):
                            
                            # 현재 시점까지의 데이터 슬라이스
                            chart_data = data.loc[:timestamp]
                            
                            # 키움 데이터 시뮬레이션
                            kiwoom_data = self.simulate_kiwoom_data(data.loc[timestamp], code)
                            
                            # 매수 전략 평가
                            portfolio_info = {
                                'holdings': self.holdings.copy(),
                                'buy_prices': self.buy_prices.copy(),
                                'buy_times': self.buy_times.copy(),
                                'highest_prices': self.highest_prices.copy(),
                                'max_holdings': self.max_holdings
                            }
                            
                            success, strategy = evaluate_strategies(
                                buy_strategies,
                                build_backtest_buy_locals(code, chart_data, portfolio_info),
                                code, "매수"
                            )
                            
                            if success:
                                self.execute_buy(code, current_prices[code], strategy['name'], timestamp)
                    
                    # 포트폴리오 가치 업데이트
                    self.update_portfolio_value(timestamp, current_prices)
                    
                    # 진행률 출력
                    if i % 100 == 0:
                        progress = (i + 1) / len(timestamps) * 100
                        logging.info(f"백테스팅 진행률: {progress:.1f}%")
                
                except Exception as ex:
                    logging.error(f"백테스팅 시점 처리 실패 ({timestamp}): {ex}")
                    continue
            
            # 최종 포트폴리오 청산
            final_timestamp = timestamps[-1]
            for code in list(self.holdings.keys()):
                if code in current_prices:
                    self.execute_sell(code, current_prices[code], "최종 청산", final_timestamp)
            
            # 결과 분석
            self.analyze_results(strategy_name)
            
            logging.info("백테스팅 완료")
            return True
            
        except Exception as ex:
            logging.error(f"백테스팅 실행 실패: {ex}")
            return False
    
    def analyze_results(self, strategy_name):
        """백테스팅 결과 분석"""
        try:
            if not self.trades or not self.equity_curve:
                logging.warning("분석할 거래 기록이나 자산 곡선이 없습니다.")
                return
            
            # 기본 통계
            total_trades = len(self.trades)
            buy_trades = [t for t in self.trades if t['action'] == 'buy']
            sell_trades = [t for t in self.trades if t['action'] == 'sell']
            
            # 수익 거래 분석
            profitable_trades = [t for t in sell_trades if t.get('profit_rate', 0) > 0]
            losing_trades = [t for t in sell_trades if t.get('profit_rate', 0) <= 0]
            
            win_rate = len(profitable_trades) / len(sell_trades) * 100 if sell_trades else 0
            
            # 평균 수익률
            avg_profit = np.mean([t.get('profit_rate', 0) for t in sell_trades]) if sell_trades else 0
            avg_profit_win = np.mean([t.get('profit_rate', 0) for t in profitable_trades]) if profitable_trades else 0
            avg_profit_loss = np.mean([t.get('profit_rate', 0) for t in losing_trades]) if losing_trades else 0
            
            # 최종 수익률
            final_value = self.cash
            total_return = (final_value - self.initial_cash) / self.initial_cash * 100
            
            # 최대 낙폭 계산
            equity_values = [point['total_value'] for point in self.equity_curve]
            if equity_values:
                peak = equity_values[0]
                max_drawdown = 0
                for value in equity_values:
                    if value > peak:
                        peak = value
                    drawdown = (peak - value) / peak * 100
                    max_drawdown = max(max_drawdown, drawdown)
            else:
                max_drawdown = 0
            
            # 결과 저장
            self.results[strategy_name] = {
                'total_trades': total_trades,
                'buy_trades': len(buy_trades),
                'sell_trades': len(sell_trades),
                'win_rate': win_rate,
                'avg_profit': avg_profit,
                'avg_profit_win': avg_profit_win,
                'avg_profit_loss': avg_profit_loss,
                'total_return': total_return,
                'max_drawdown': max_drawdown,
                'final_value': final_value,
                'initial_cash': self.initial_cash,
                'trades': self.trades,
                'equity_curve': self.equity_curve
            }
            
            # 결과 출력
            logging.info(f"\n=== 백테스팅 결과: {strategy_name} ===")
            logging.info(f"총 거래 수: {total_trades}")
            logging.info(f"매수 거래: {len(buy_trades)}")
            logging.info(f"매도 거래: {len(sell_trades)}")
            logging.info(f"승률: {win_rate:.2f}%")
            logging.info(f"평균 수익률: {avg_profit:.2f}%")
            logging.info(f"평균 수익 (승리): {avg_profit_win:.2f}%")
            logging.info(f"평균 수익 (손실): {avg_profit_loss:.2f}%")
            logging.info(f"총 수익률: {total_return:.2f}%")
            logging.info(f"최대 낙폭: {max_drawdown:.2f}%")
            logging.info(f"최종 자산: {final_value:,.0f}원")
            
        except Exception as ex:
            logging.error(f"결과 분석 실패: {ex}")
    
    def plot_results(self, strategy_name):
        """결과 차트 생성"""
        try:
            if strategy_name not in self.results:
                logging.error(f"결과를 찾을 수 없음: {strategy_name}")
                return
            
            result = self.results[strategy_name]
            equity_curve = result['equity_curve']
            
            if not equity_curve:
                logging.warning("자산 곡선 데이터가 없습니다.")
                return
            
            # 차트 생성
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10))
            
            # 자산 곡선
            timestamps = [point['timestamp'] for point in equity_curve]
            values = [point['total_value'] for point in equity_curve]
            
            ax1.plot(timestamps, values, label='총 자산', linewidth=2)
            ax1.axhline(y=self.initial_cash, color='r', linestyle='--', label='초기 자금')
            ax1.set_title(f'{strategy_name} - 자산 곡선')
            ax1.set_ylabel('자산 (원)')
            ax1.legend()
            ax1.grid(True, alpha=0.3)
            
            # 수익률 곡선
            returns = [(value - self.initial_cash) / self.initial_cash * 100 for value in values]
            ax2.plot(timestamps, returns, label='수익률', linewidth=2, color='green')
            ax2.axhline(y=0, color='r', linestyle='--')
            ax2.set_title(f'{strategy_name} - 수익률 곡선')
            ax2.set_xlabel('날짜')
            ax2.set_ylabel('수익률 (%)')
            ax2.legend()
            ax2.grid(True, alpha=0.3)
            
            plt.tight_layout()
            
            # 차트 저장
            chart_filename = f"backtest_result_{strategy_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            plt.savefig(chart_filename, dpi=300, bbox_inches='tight')
            plt.show()
            
            logging.info(f"차트 저장: {chart_filename}")
            
        except Exception as ex:
            logging.error(f"차트 생성 실패: {ex}")
    
    def export_results(self, strategy_name, filename=None):
        """결과를 Excel 파일로 내보내기"""
        try:
            if strategy_name not in self.results:
                logging.error(f"결과를 찾을 수 없음: {strategy_name}")
                return
            
            if filename is None:
                filename = f"backtest_result_{strategy_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            
            result = self.results[strategy_name]
            
            # Excel 파일 생성
            with pd.ExcelWriter(filename, engine='openpyxl') as writer:
                # 거래 기록
                trades_df = pd.DataFrame(result['trades'])
                trades_df.to_excel(writer, sheet_name='거래기록', index=False)
                
                # 자산 곡선
                equity_df = pd.DataFrame(result['equity_curve'])
                equity_df.to_excel(writer, sheet_name='자산곡선', index=False)
                
                # 요약 통계
                summary_data = {
                    '항목': [
                        '총 거래 수', '매수 거래', '매도 거래', '승률 (%)', 
                        '평균 수익률 (%)', '평균 수익 (승리) (%)', '평균 수익 (손실) (%)',
                        '총 수익률 (%)', '최대 낙폭 (%)', '최종 자산 (원)', '초기 자금 (원)'
                    ],
                    '값': [
                        result['total_trades'], result['buy_trades'], result['sell_trades'],
                        result['win_rate'], result['avg_profit'], result['avg_profit_win'],
                        result['avg_profit_loss'], result['total_return'], result['max_drawdown'],
                        result['final_value'], result['initial_cash']
                    ]
                }
                summary_df = pd.DataFrame(summary_data)
                summary_df.to_excel(writer, sheet_name='요약통계', index=False)
            
            logging.info(f"결과 내보내기 완료: {filename}")
            
        except Exception as ex:
            logging.error(f"결과 내보내기 실패: {ex}")
    
    def reset_portfolio(self):
        """포트폴리오 초기화"""
        self.cash = self.initial_cash
        self.holdings.clear()
        self.buy_prices.clear()
        self.buy_times.clear()
        self.highest_prices.clear()
        self.trades.clear()
        self.equity_curve.clear()
        
        logging.info("포트폴리오 초기화 완료")
    
    def check_available_data(self, code=None):
        """데이터베이스에서 사용 가능한 데이터 확인"""
        try:
            conn = sqlite3.connect(self.db_path)
            
            # 사용 가능한 종목과 데이터 타입 확인
            available_data = {
                'integrated_data': {},
                'tick_data': {},
                'minute_data': {},
                'trade_records': {}
            }
            
            # 통합 데이터 확인 (우선순위)
            integrated_query = """
                SELECT code, COUNT(*) as count, 
                       MIN(datetime) as start_date, 
                       MAX(datetime) as end_date
                FROM stock_data 
                GROUP BY code
            """
            if code:
                integrated_query = """
                    SELECT code, COUNT(*) as count, 
                           MIN(datetime) as start_date, 
                           MAX(datetime) as end_date
                    FROM stock_data 
                    WHERE code = ?
                    GROUP BY code
                """
                integrated_df = pd.read_sql_query(integrated_query, conn, params=(code,))
            else:
                integrated_df = pd.read_sql_query(integrated_query, conn)
            
            for _, row in integrated_df.iterrows():
                available_data['integrated_data'][row['code']] = {
                    'count': row['count'],
                    'start_date': row['start_date'],
                    'end_date': row['end_date']
                }
            
            # 기존 테이블들도 확인 (백워드 호환성)
            try:
                # 틱 데이터 확인
                tick_query = """
                    SELECT code, COUNT(*) as count, 
                           MIN(timestamp) as start_date, 
                           MAX(timestamp) as end_date
                    FROM tick_data 
                    GROUP BY code
                """
                if code:
                    tick_query = """
                        SELECT code, COUNT(*) as count, 
                               MIN(timestamp) as start_date, 
                               MAX(timestamp) as end_date
                        FROM tick_data 
                        WHERE code = ?
                        GROUP BY code
                    """
                    tick_df = pd.read_sql_query(tick_query, conn, params=(code,))
                else:
                    tick_df = pd.read_sql_query(tick_query, conn)
                
                for _, row in tick_df.iterrows():
                    available_data['tick_data'][row['code']] = {
                        'count': row['count'],
                        'start_date': row['start_date'],
                        'end_date': row['end_date']
                    }
            except Exception:
                pass  # tick_data 테이블이 없을 수 있음
            
            try:
                # 분봉 데이터 확인
                minute_query = """
                    SELECT code, COUNT(*) as count, 
                           MIN(timestamp) as start_date, 
                           MAX(timestamp) as end_date
                    FROM minute_data 
                    GROUP BY code
                """
                if code:
                    minute_query = """
                        SELECT code, COUNT(*) as count, 
                               MIN(timestamp) as start_date, 
                               MAX(timestamp) as end_date
                        FROM minute_data 
                        WHERE code = ?
                        GROUP BY code
                    """
                    minute_df = pd.read_sql_query(minute_query, conn, params=(code,))
                else:
                    minute_df = pd.read_sql_query(minute_query, conn)
                
                for _, row in minute_df.iterrows():
                    available_data['minute_data'][row['code']] = {
                        'count': row['count'],
                        'start_date': row['start_date'],
                        'end_date': row['end_date']
                    }
            except Exception:
                pass  # minute_data 테이블이 없을 수 있음
            
            # 거래 기록 확인
            try:
                trade_query = """
                    SELECT code, COUNT(*) as count, 
                           MIN(datetime) as start_date, 
                           MAX(datetime) as end_date
                    FROM trade_records 
                    GROUP BY code
                """
                if code:
                    trade_query = """
                        SELECT code, COUNT(*) as count, 
                               MIN(datetime) as start_date, 
                               MAX(datetime) as end_date
                        FROM trade_records 
                        WHERE code = ?
                        GROUP BY code
                    """
                    trade_df = pd.read_sql_query(trade_query, conn, params=(code,))
                else:
                    trade_df = pd.read_sql_query(trade_query, conn)
                
                for _, row in trade_df.iterrows():
                    available_data['trade_records'][row['code']] = {
                        'count': row['count'],
                        'start_date': row['start_date'],
                        'end_date': row['end_date']
                    }
            except Exception:
                pass  # trade_records 테이블이 없을 수 있음
            
            conn.close()
            
            # 결과 출력
            logging.info("=== 사용 가능한 데이터 ===")
            
            if available_data['integrated_data']:
                logging.info(f"통합 데이터: {len(available_data['integrated_data'])}개 종목")
                for code, info in available_data['integrated_data'].items():
                    logging.info(f"  {code}: {info['count']}개 레코드 ({info['start_date']} ~ {info['end_date']})")
            else:
                logging.info("통합 데이터: 없음")
            
            if available_data['tick_data']:
                logging.info(f"틱 데이터: {len(available_data['tick_data'])}개 종목")
                for code, info in available_data['tick_data'].items():
                    logging.info(f"  {code}: {info['count']}개 레코드 ({info['start_date']} ~ {info['end_date']})")
            else:
                logging.info("틱 데이터: 없음")
            
            if available_data['minute_data']:
                logging.info(f"분봉 데이터: {len(available_data['minute_data'])}개 종목")
                for code, info in available_data['minute_data'].items():
                    logging.info(f"  {code}: {info['count']}개 레코드 ({info['start_date']} ~ {info['end_date']})")
            else:
                logging.info("분봉 데이터: 없음")
            
            if available_data['trade_records']:
                logging.info(f"거래 기록: {len(available_data['trade_records'])}개 종목")
                for code, info in available_data['trade_records'].items():
                    logging.info(f"  {code}: {info['count']}개 거래 ({info['start_date']} ~ {info['end_date']})")
            else:
                logging.info("거래 기록: 없음")
            
            return available_data
            
        except Exception as ex:
            logging.error(f"데이터 확인 실패: {ex}")
            return {}

def main():
    """백테스터 실행 예제"""
    try:
        # 로깅 설정
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        
        # 백테스터 생성
        backtester = KiwoomBacktester('stock_data.db', 'settings.ini', 10000000)
        
        # 사용 가능한 데이터 확인
        available_data = backtester.check_available_data()
        
        # 백테스팅할 종목 리스트 (실제 데이터가 있는 종목만)
        codes = []
        if available_data['integrated_data']:
            codes.extend(list(available_data['integrated_data'].keys())[:3])  # 최대 3개 종목
        elif available_data['tick_data']:
            codes.extend(list(available_data['tick_data'].keys())[:3])  # 최대 3개 종목
        elif available_data['minute_data']:
            codes.extend(list(available_data['minute_data'].keys())[:3])  # 최대 3개 종목
        
        if not codes:
            logging.warning("백테스팅할 데이터가 없습니다.")
            return
        
        logging.info(f"백테스팅 대상 종목: {codes}")
        
        # 백테스팅 기간 (실제 데이터 범위에 맞춰 조정)
        start_date = '2024-01-01'
        end_date = '2024-12-31'
        
        # 전략별 백테스팅 실행
        strategies = ['통합 전략', '급등주', '갭상승']
        
        for strategy in strategies:
            if strategy in backtester.strategies:
                logging.info(f"\n{'='*50}")
                logging.info(f"백테스팅 시작: {strategy}")
                logging.info(f"{'='*50}")
                
                # 백테스팅 실행 (틱 데이터 우선)
                success = backtester.run_backtest(codes, start_date, end_date, strategy, 'auto')
                
                if success:
                    # 결과 차트 생성
                    backtester.plot_results(strategy)
                    
                    # 결과 내보내기
                    backtester.export_results(strategy)
                
                # 포트폴리오 초기화
                backtester.reset_portfolio()
        
        logging.info("모든 백테스팅 완료")
        
    except Exception as ex:
        logging.error(f"백테스터 실행 실패: {ex}")

if __name__ == "__main__":
    main()
