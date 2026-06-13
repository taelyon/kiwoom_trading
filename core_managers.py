"""
UI 매니저 모듈 (통합 버전)

기존에 개별 파일로 분리되어 있던 매니저 클래스들을 하나의 모듈로 통합하였습니다.
- LoginHandler: 로그인 및 연결 관리
- DataManager: 데이터 조회 및 정규화 관리
- MonitoringManager: 실시간 모니터링 종목 관리
- StrategyManager: 투자 전략 설정 및 로드/저장 관리
- TradingManager: 매매 주문 및 수동 거래 실행 관리
- AccountManager: 계좌 잔고 및 예수금 관리
- ConditionSearchManager: 조건검색식 목록 및 실시간 조건검색 실행 관리
- MLManager: 머신러닝 모델 학습 스케줄 및 워커 관리
"""

import logging
import asyncio
import ast
import json
import time
from datetime import datetime, timedelta

from config_manager import EnvConfigParser
from kiwoom_rest import KiwoomRestClient
from kiwoom_websocket import KiwoomWebSocketClient
from trader import KiwoomTrader
from strategy import KiwoomStrategy
from ml_trainer import MLTrainingWorker
from utils import create_fire_and_forget_task


# ==========================================
# 1. LoginHandler (로그인 및 연결 관리)
# ==========================================
class LoginHandler:
    """로그인 및 연결 관리 클래스 (Pure Python)"""
    
    def __init__(self, parent_app):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent_app
        self.config = EnvConfigParser()
        self.kiwoom_client = None
        self.websocket_client = None
        self.websocket_task = None
    
    def get_current_holdings_count(self):
        """현재 보유종목 수 조회"""
        try:
            # 1차: KiwoomTrader의 holdings 확인
            if hasattr(self.parent, 'trader') and self.parent.trader:
                if hasattr(self.parent.trader, 'holdings'):
                    count = len(self.parent.trader.holdings)
                    self.logger.debug(f"📊 보유종목 수 (trader.holdings): {count}개")
                    return count
            
            # 2차: 웹소켓 실시간 잔고 데이터 확인
            if hasattr(self, 'websocket_client') and self.websocket_client:
                balance_data = self.websocket_client.balance_data
                active_holdings = {code: info for code, info in balance_data.items() 
                                 if info.get('quantity', 0) > 0}
                count = len(active_holdings)
                self.logger.debug(f"📊 보유종목 수 (balance_data): {count}개")
                return count
            return 0
        except Exception as ex:
            self.logger.error(f"보유종목 수 조회 실패: {ex}", exc_info=True)
            return 0
    
    def get_available_buy_count(self):
        """매수가능 종목수 계산"""
        try:
            # 설정 파일에서 목표 매수 종목 수 로드
            max_count = self.config.getint('SETTINGS', 'buycount', fallback=5)
            current_count = self.get_current_holdings_count()
            available_count = max(0, max_count - current_count)
            self.logger.debug(f"📊 투자 종목 현황: 최대 {max_count}종목, 현재 보유 {current_count}종목, 매수가능 {available_count}종목")
            return available_count
        except Exception as ex:
            self.logger.error(f"매수가능 종목수 계산 실패: {ex}", exc_info=True)
            return 1
    
    def load_settings_sync(self):
        """설정 로드 (CLI 환경 호환)"""
        pass
    
    async def save_settings(self):
        """설정 저장"""
        try:
            self.save_settings_sync()
        except Exception as ex:
            self.logger.error(f"설정 저장 실패: {ex}", exc_info=True)
    
    def save_settings_sync(self, settings_dict=None):
        """설정 저장"""
        try:
            if settings_dict:
                for section, options in settings_dict.items():
                    for option, value in options.items():
                        self.config.set(section, option, str(value))
                self.config.save()
                self.logger.debug("✅ 설정 내용이 .env에 저장되었습니다.")
        except Exception as ex:
            self.logger.error(f"설정 저장 중 오류 발생: {ex}", exc_info=True)
    
    async def start_websocket_client(self):
        """웹소켓 클라이언트 시작"""
        try:           
            if self.kiwoom_client is None:
                self.logger.error("❌ 키움 클라이언트가 초기화되지 않았습니다. 먼저 API 연결을 시도해주세요.")
                return
            
            if hasattr(self, 'websocket_client') and self.websocket_client and self.websocket_client.connected:
                self.logger.info("✅ 웹소켓 클라이언트가 이미 연결되어 있습니다 (재사용)")
                return
            
            existing_balance_data = {}
            if hasattr(self, 'websocket_client') and self.websocket_client and hasattr(self.websocket_client, 'balance_data'):
                existing_balance_data = dict(self.websocket_client.balance_data)
            
            token = self.kiwoom_client.access_token
            is_mock = self.kiwoom_client.is_mock
            logger = logging.getLogger('KiwoomWebSocketClient')
            
            self.websocket_client = KiwoomWebSocketClient(token, logger, is_mock, self.parent)
            
            if existing_balance_data:
                self.websocket_client.balance_data = existing_balance_data
            
            self.websocket_task = asyncio.create_task(self.websocket_client.run())
        except Exception as e:
            self.logger.error(f"웹소켓 클라이언트 시작 실패: {e}", exc_info=True)
            
    async def init_kiwoom_client(self):
        """키움 REST API 클라이언트 초기화 (비동기)"""
        try:
            client = KiwoomRestClient('.env')
            if await client.connect():
                is_expired = False
                if hasattr(client, 'is_token_expired'):
                    is_expired = client.is_token_expired()
                elif hasattr(client, 'access_token_expired'):
                     try:
                        expired_time = client.access_token_expired
                        if isinstance(expired_time, str):
                            expired_time = datetime.strptime(expired_time, '%Y-%m-%d %H:%M:%S')
                        
                        if isinstance(expired_time, datetime):
                            if datetime.now() >= expired_time - timedelta(minutes=1):
                                  is_expired = True
                     except Exception:
                          pass

                if is_expired:
                    self.logger.info("KiwoomRestClient 연결 직후 토큰 만료 감지 - 즉시 갱신 시도")
                    if await client.get_access_token():
                        self.logger.info("✅ 초기 토큰 갱신 성공")
                    else:
                        self.logger.error("❌ 초기 토큰 갱신 실패")
                        return None
                
                return client
            else:
                self.logger.error("키움 REST API 클라이언트 초기화 실패")
                return None
        except Exception as ex:
            self.logger.error(f"키움 REST API 클라이언트 초기화 중 오류: {ex}", exc_info=True)
            return None
    
    async def handle_api_connection(self):
        """키움 REST API 연결 처리 (비동기)"""
        try:
            self.kiwoom_client = await self.init_kiwoom_client()
            
            if self.kiwoom_client and self.kiwoom_client.is_connected:
                self.parent.update_connection_status(True)
                
                is_simulation = self.config.getboolean('KIWOOM_API', 'simulation', fallback=True)
                mode = "모의투자" if is_simulation else "실전투자"
                logging.debug(f"키움 REST API 연결 성공! 거래 모드: {mode}")
                
                try:
                    if not hasattr(self.parent, 'trader') or not self.parent.trader:
                        buycount = self.config.getint('SETTINGS', 'buycount', fallback=5)
                        self.parent.trader = KiwoomTrader(self.kiwoom_client, buycount, self.parent)
                        
                        self.parent.objstg = KiwoomStrategy(self.parent.trader, self.parent)
                        self.logger.debug("✅ 전략 객체(KiwoomStrategy) 생성 완료")

                        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                            self.parent.chart_cache.trader = self.parent.trader
                except Exception as trader_ex:
                    self.logger.error(f"트레이더 객체 생성 실패: {trader_ex}", exc_info=True)
            else:
                self.logger.error("키움 REST API 연결 실패! .env 파일의 appkey와 appsecret을 확인해주세요.")
                self.parent.update_connection_status(False)
        except Exception as ex:
            self.logger.error(f"API 연결 처리 실패: {ex}", exc_info=True)
    
    async def _handle_connection_toggle_async(self):
        """연결/해제 버튼 클릭 비동기 처리"""
        try:
            is_connected = (hasattr(self, 'kiwoom_client') and 
                            self.kiwoom_client and 
                            self.kiwoom_client.is_connected)

            if is_connected:
                self.logger.info("🔌 API 연결 해제를 시도합니다...")
                if hasattr(self, 'websocket_client') and self.websocket_client:
                    await self.websocket_client.stop()
                if self.kiwoom_client:
                    await self.kiwoom_client.disconnect()
                
                self.parent.update_connection_status(False)
                self.logger.info("✅ API 연결이 해제되었습니다.")
            else:
                self.logger.info("🔌 API 연결을 시도합니다...")
                await self.handle_api_connection()
                await self.start_websocket_client()
                self.parent.update_connection_status(True)
        except Exception as ex:
            self.logger.error(f"연결/해제 처리 중 오류: {ex}", exc_info=True)


# ==========================================
# 2. DataManager (데이터 조회 및 관리)
# ==========================================
class DataManager:
    """데이터 조회 및 관리 매니저"""
    
    def __init__(self, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
        self.stock_code_map = {}  # 종목명: 종목코드 캐시
        self._last_cache_attempt_time = 0  # 전체 캐싱 시도 시간 기록
        self._is_caching = False  # 현재 캐싱 작업 진행 중 여부 플래그
        self.non_existent_codes = set()  # 캐시에 존재하지 않는 것으로 판명된 특수/잘못된 코드(Negative Cache)
    
    def safe_int(self, value, default=0):
        """안전한 정수 변환"""
        try:
            if value is None or value == '':
                return default
            return int(float(str(value).replace(',', '')))
        except (ValueError, TypeError):
            return default
    
    def safe_float(self, value, default=0.0):
        """안전한 실수 변환"""
        try:
            if value is None or value == '':
                return default
            return float(str(value).replace(',', ''))
        except (ValueError, TypeError):
            return default
    
    def normalize_stock_code(self, code):
        """종목코드 정규화 (앞의 'A' 제거)"""
        try:
            if not code:
                return ""
            
            # 문자열로 변환
            code_str = str(code).strip()
            
            # 'A'로 시작하면 제거
            if code_str.startswith('A') or code_str.startswith('a'):
                code_str = code_str[1:]
            
            # 6자리 종목코드로 정규화 (앞에 0 채우기)
            code_str = code_str.zfill(6)
            
            return code_str
            
        except Exception as ex:
            self.logger.error(f"종목코드 정규화 실패 ({code}): {ex}")
            return str(code) if code else ""
    
    async def normalize_stock_input(self, stock_input):
        """종목 입력값을 정규화하여 종목코드와 종목명 반환"""
        try:
            # 숫자만 있는 경우 (종목코드)
            if stock_input.isdigit():
                if len(stock_input) == 6:
                    # 종목코드만 반환 (API 호출 제거)
                    return stock_input, f"종목{stock_input}"
                else:
                    # 6자리가 아닌 경우 앞에 0을 붙여서 6자리로 만듦
                    stock_code = stock_input.zfill(6)
                    return stock_code, f"종목{stock_code}"
            
            # 한글이 포함된 경우 (종목명)
            else:
                # 종목명으로 종목코드 조회
                stock_code = await self.get_stock_code_by_name(stock_input)
                if stock_code:
                    return stock_code, stock_input
                else:
                    return None, None

        except Exception as ex:
            self.logger.error(f"종목 입력 정규화 실패: {ex}")
            return None, None
    
    def get_stock_name_by_code(self, stock_code):
        """종목코드로 종목명 조회 (캐시 맵 역방향 조회 적용 및 자가치유 갱신 도입)"""
        if not stock_code:
            return "알수없음"
        
        code_str = str(stock_code).strip().zfill(6)
        
        try:
            if self.stock_code_map:
                for name, code in self.stock_code_map.items():
                    if str(code).strip().zfill(6) == code_str:
                        return name
            
            # 캐시에 없고, 이미 존재하지 않는 코드로 분류되지 않았으며, 현재 캐싱 작업 진행 중이 아닐 때
            if code_str not in self.non_existent_codes and not self._is_caching:
                now = time.time()
                # 마지막 수집 시도 후 30초가 지났다면 백그라운드 갱신 작동
                if (not self.stock_code_map or now - self._last_cache_attempt_time > 30.0):
                    self._last_cache_attempt_time = now
                    self.logger.info(f"🔍 종목코드 '{code_str}'의 캐시 부재로 전체 종목 캐시 백그라운드 갱신 요청")
                    
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                    except RuntimeError:
                        loop = None
                    
                    if loop and loop.is_running():
                        loop.create_task(self._cache_all_stock_codes_async(requested_code=code_str))
                    
        except Exception as e:
            self.logger.error(f"get_stock_name_by_code 예외 발생: {e}")
            
        return f"종목{stock_code}"
    
    async def get_stock_code_by_name(self, stock_name):
        """종목명으로 종목코드 조회 - ka10099 API로 전체 목록을 받아와서 검색"""
        # 1. 캐시에서 먼저 조회
        if self.stock_code_map and stock_name in self.stock_code_map:
            stock_code = self.stock_code_map[stock_name]
            self.logger.info(f"✅ 종목명 캐시 조회 성공: {stock_name} -> {stock_code}")
            return stock_code

        # 2. 캐시에 없으면 API를 통해 조회 (Fallback)
        self.logger.warning(f"⚠️ 캐시에 '{stock_name}'이(가) 없어 API로 조회합니다.")
        try:
            if hasattr(self.parent, 'login_handler') and self.parent.login_handler.kiwoom_client:
                kiwoom_client = self.parent.login_handler.kiwoom_client

                # 코스피(0), 코스닥(10), K-OTC(30), 코넥스(50) 종목 리스트를 모두 조회
                for market_code in ['0', '10', '30', '50']:
                    headers = {
                        'Content-Type': 'application/json;charset=UTF-8',
                        'authorization': f'Bearer {kiwoom_client.access_token}',
                        'api-id': 'ka10099',
                    }
                    server_url = kiwoom_client.mock_url if kiwoom_client.is_mock else kiwoom_client.base_url
                    data = {'mrkt_tp': market_code}
                    url = f"{server_url}/api/dostk/stkinfo"
                    
                    response = await kiwoom_client.client.post(url, headers=headers, json=data, timeout=10.0)

                    if response.status_code == 200:
                        result = response.json()
                        if result.get('return_code') == 0 and 'list' in result:
                            for stock_info in result['list']:
                                if stock_info.get('name') == stock_name:
                                    stock_code = stock_info.get('code')
                                    if stock_code:
                                        self.logger.info(f"✅ 종목명 API 검색 성공: {stock_name} -> {stock_code}")
                                        # 찾은 종목을 캐시에 추가
                                        self.stock_code_map[stock_name] = stock_code
                                        return stock_code
                        else:
                            self.logger.warning(f"종목 리스트 조회 실패 (시장: {market_code}): {result.get('return_msg')}")
                    else:
                        self.logger.error(f"종목 리스트 API 호출 실패 (시장: {market_code}): HTTP {response.status_code}")
            return None
        except Exception as ex:
            self.logger.error(f"종목명 검색 실패 ({stock_name}): {ex}")
            return None

    async def _cache_all_stock_codes_async(self, requested_code=None):
        """프로그램 시작 시 전체 종목 코드를 메모리에 캐싱 (안정성 강화 버전)"""
        if self._is_caching:
            return
        self._is_caching = True
        
        try:
            if hasattr(self.parent, 'login_handler') and self.parent.login_handler.kiwoom_client:
                kiwoom_client = self.parent.login_handler.kiwoom_client
                
                # API 호출 전 토큰 유효성 체크 및 갱신 시도
                if hasattr(kiwoom_client, 'check_token_validity'):
                    await kiwoom_client.check_token_validity()
                
                temp_map = {}
                markets = ['0', '10', '30', '50'] # KOSPI, KOSDAQ, K-OTC, KONEX
                for idx, market_code in enumerate(markets):
                    # KOSPI와 KOSDAQ 목록 수신 간 1초 지연을 적용하여 HTTP 429 Too Many Requests 방지
                    if idx > 0:
                        await asyncio.sleep(1.0)
                        
                    headers = {
                        'Content-Type': 'application/json;charset=UTF-8', 
                        'authorization': f'Bearer {kiwoom_client.access_token}', 
                        'api-id': 'ka10099'
                    }
                    server_url = kiwoom_client.mock_url if kiwoom_client.is_mock else kiwoom_client.base_url
                    data = {'mrkt_tp': market_code}
                    url = f"{server_url}/api/dostk/stkinfo"
                    
                    response = await kiwoom_client.client.post(url, headers=headers, json=data, timeout=30.0)
                    if response.status_code == 200:
                        result = response.json()
                        if result.get('return_code') == 0 and 'list' in result:
                            for stock_info in result['list']:
                                name = stock_info.get('name')
                                code = stock_info.get('code')
                                if name and code:
                                    temp_map[name] = str(code).strip().zfill(6)
                        else:
                            self.logger.warning(f"⚠️ 종목 마스터 리스트 수신 실패 (시장코드: {market_code}, 원인: {result.get('return_msg', '알수없음')})")
                    elif response.status_code == 429:
                        self.logger.error(f"🚨 Too Many Requests HTTP 429 감지 (시장코드: {market_code}). 잠시 후 재시도합니다.")
                    else:
                        self.logger.warning(f"⚠️ 종목 마스터 리스트 HTTP 에러 (시장코드: {market_code}, 코드: {response.status_code})")
                
                # 성공적으로 데이터를 받아왔을 때만 기존 맵을 덮어씀 (일시적 실패 시 기존 캐시 보존)
                if temp_map:
                    self.stock_code_map = temp_map
                    self.logger.info(f"✅ 전체 종목 코드 캐싱 완료: {len(self.stock_code_map)}개 종목")
                    
                    # 갱신 완료 후에도 요청된 코드가 없는 경우 캐시 예외 목록(Negative Cache)에 등록
                    if requested_code:
                        found = False
                        req_clean = str(requested_code).strip().zfill(6)
                        for name, code in self.stock_code_map.items():
                            if code == req_clean:
                                found = True
                                break
                        if not found:
                            self.non_existent_codes.add(req_clean)
                            self.logger.info(f"🚫 종목코드 '{req_clean}'은 OpenAPI 마스터 목록에 없으므로 캐시 예외 목록(Negative Cache)에 등록합니다.")
                        else:
                            # 만약 예외 목록에 등록되었던 종목이 신규로 발견되었을 경우, 예외 목록에서 제거(discard)하여 정상 상태로 환원
                            self.non_existent_codes.discard(req_clean)
                            self.logger.info(f"🔄 종목코드 '{req_clean}'이 캐시에서 발견되어 예외 목록(Negative Cache)에서 제외했습니다.")
                else:
                    self.logger.warning("⚠️ 받아온 종목 리스트가 없어 기존 종목명 캐시를 유지합니다.")
        except Exception as ex:
            self.logger.error(f"전체 종목 코드 캐싱 실패: {ex}", exc_info=True)
        finally:
            self._is_caching = False


# ==========================================
# 3. MonitoringManager (종목 모니터링 관리)
# ==========================================
class MonitoringManager:
    """모니터링 종목 관리 매니저 (Pure Python)"""
    
    def __init__(self, parent_app):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent_app
        self.monitored_stocks = set()  # 모니터링할 종목코드 집합
        self.stock_added_time = {}     # 종목 편입 시간 기록 (TTL 체크용)
        self.monitoring_ttl_minutes = 60  # 기본 TTL 60분
        self.max_monitored_stocks = 50  # 최대 감시 종목 수 제한
    
    async def add_stock_to_monitoring(self, code, name=None):
        """모니터링 종목 추가"""
        try:
            if code in self.monitored_stocks:
                self.logger.debug(f"종목이 이미 모니터링 목록에 있습니다: {code}")
                return True
            
            # 최대 감시 종목 수 제한 체크 및 오래된 종목 밀어내기
            if len(self.monitored_stocks) >= self.max_monitored_stocks:
                # 보유 종목은 밀어내기 대상에서 제외
                holding_codes = set()
                if hasattr(self.parent, 'account_manager') and self.parent.account_manager:
                    holding_codes = set(self.parent.account_manager.holdings.keys())
                
                # 보유 종목이 아닌 종목 중 가장 오래된 종목 찾기
                removable_stocks = [c for c in self.monitored_stocks if c not in holding_codes]
                
                if removable_stocks:
                    # 추가된 시간이 가장 오래된 종목 정렬
                    oldest_code = min(removable_stocks, key=lambda c: self.stock_added_time.get(c, datetime.min))
                    self.logger.info(f"🔄 최대 감시 종목({self.max_monitored_stocks}개) 초과로 가장 오래된 종목 자동 밀어내기: {oldest_code}")
                    await self.remove_stock_from_monitoring(oldest_code)
                else:
                    self.logger.warning(f"⚠️ 모든 감시 종목이 보유 종목이어서 밀어낼 수 없습니다. 추가 취소: {code}")
                    return False
            
            # 집합에 추가
            self.monitored_stocks.add(code)
            self.stock_added_time[code] = datetime.now()
            self.logger.debug(f"✅ 모니터링 종목 추가: {code}")
            
            # 차트 캐시에 추가
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                await self.parent.chart_cache.add_monitoring_stock(code)

            # 모니터링 종목 수 변경에 따른 차트 업데이트 주기 조절
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                self.parent.chart_cache.update_chart_update_interval()
            
            # 실시간 체결 데이터 구독
            ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
            if ws_client and ws_client.connected:
                try:
                    asyncio.get_running_loop()  # 루프 확인
                    create_fire_and_forget_task(ws_client.subscribe_stock_execution_data([code], 'monitoring'))
                    self.logger.debug(f"📡 실시간 체결 데이터 구독: {code}")
                except RuntimeError:
                    pass
            
            return True
        except Exception as ex:
            self.logger.error(f"모니터링 종목 추가 실패 ({code}): {ex}", exc_info=True)
            return False
    
    async def add_stock_to_monitoring_async(self, code):
        """모니터링 종목 추가 (비동기 버전)"""
        try:
            if code in self.monitored_stocks:
                self.logger.debug(f"종목이 이미 모니터링 목록에 있습니다: {code}")
                return True
            
            self.monitored_stocks.add(code)
            self.stock_added_time[code] = datetime.now()
            self.logger.debug(f"✅ 모니터링 종목 추가: {code}")
            
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                await self.parent.chart_cache.add_monitoring_stock(code)

            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                self.parent.chart_cache.update_chart_update_interval()
            
            ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
            if ws_client and ws_client.connected:
                await ws_client.subscribe_stock_execution_data([code], 'monitoring')
            return True
        except Exception as ex:
            self.logger.error(f"모니터링 종목 추가 실패 (async) ({code}): {ex}")
            return False

    async def remove_stock_from_monitoring(self, code):
        """모니터링 종목 제거"""
        try:
            # 보유 중인 종목인지 확인
            is_held = False
            trader = getattr(self.parent, 'trader', None)
            if trader and hasattr(trader, 'holdings'):
                if code in trader.holdings and trader.holdings[code].get('quantity', 0) > 0:
                    is_held = True
            
            if is_held:
                self.logger.info(f"🛡️ {code}는 보유 중인 종목이므로 모니터링 목록에서 제거하지 않습니다.")
                return True
            
            # 집합에서 제거
            if code in self.monitored_stocks:
                self.monitored_stocks.discard(code)
                self.stock_added_time.pop(code, None)
                self.logger.debug(f"✅ 모니터링 종목 제거: {code}")

            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                self.parent.chart_cache.remove_monitoring_stock(code)
            
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                self.parent.chart_cache.update_chart_update_interval()

            # 실시간 체결 데이터 구독 해제
            ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
            if ws_client and ws_client.connected:
                try:
                    asyncio.get_running_loop()  # 루프 확인
                    create_fire_and_forget_task(ws_client.unsubscribe_stock_execution_data([code]))
                    self.logger.debug(f"📡 실시간 구독 해제: {code}")
                except RuntimeError:
                    pass
            
            return True
        except Exception as ex:
            self.logger.error(f"모니터링 종목 제거 실패 ({code}): {ex}")
            return False
    
    async def remove_condition_stocks_from_monitoring(self, seq):
        """조건검색으로 추가된 종목들을 모니터링에서 제거"""
        try:
            if not hasattr(self.parent, 'condition_search_results') or seq not in self.parent.condition_search_results:
                self.logger.debug(f"조건검색 결과 없음 (seq: {seq})")
                return
            
            stock_codes = self.parent.condition_search_results.get(seq, [])
            self.logger.info(f"조건검색 종목 제거 시작: {len(stock_codes)}개 (seq: {seq})")
            
            for code in stock_codes:
                await self.remove_stock_from_monitoring(code)

            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                self.parent.chart_cache.update_chart_update_interval()
            
            del self.parent.condition_search_results[seq]
            self.logger.info(f"조건검색 종목 제거 완료 (seq: {seq})")
        except Exception as ex:
            self.logger.error(f"조건검색 종목 제거 실패 (seq: {seq}): {ex}", exc_info=True)
    
    def extract_monitoring_stock_codes_enhanced(self):
        """모니터링 종목 코드 추출 및 차트 캐시 업데이트"""
        try:
            monitoring_codes = self.get_monitoring_stock_codes()
            self.logger.debug(f"📋 모니터링 종목: {monitoring_codes}")
            
            # 차트 데이터 캐시 업데이트
            try:
                if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                    self.parent.chart_cache.update_monitoring_stocks(monitoring_codes)
                else:
                    self.logger.warning("⚠️ 차트 캐시가 초기화되지 않았습니다")
            except Exception as cache_ex:
                self.logger.error(f"❌ 차트 캐시 업데이트 실패: {cache_ex}", exc_info=True)
            
            return monitoring_codes
        except Exception as ex:
            self.logger.error(f"❌ 모니터링 종목 코드 추출 실패: {ex}", exc_info=True)
            return []
    
    def get_monitoring_stock_codes(self):
        """모니터링+보유 종목 코드 리스트 추출 (통합 버전)"""
        try:
            stock_codes = set(self.monitored_stocks)
            
            # 보유 종목 추출
            trader = getattr(self.parent, 'trader', None)
            if trader and hasattr(trader, 'holdings'):
                for code, info in trader.holdings.items():
                    if info.get('quantity', 0) > 0:
                        clean_code = code[1:] if code.startswith('A') else code
                        if clean_code and clean_code.isdigit() and len(clean_code) == 6:
                            stock_codes.add(clean_code)
            
            result_list = list(stock_codes)
            return result_list
        except Exception as ex:
            self.logger.error(f"모니터링 종목 코드 추출 실패: {ex}")
            return []
    
    def subscribe_realtime_execution_data(self, code):
        """실시간 체결 데이터 구독 시작"""
        try:
            ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
            if ws_client and ws_client.connected:
                try:
                    asyncio.get_running_loop()
                    create_fire_and_forget_task(ws_client.subscribe_stock_execution_data([code], 'monitoring'))
                    self.logger.debug(f"📡 모니터링 종목 실시간 체결(0B) 구독 요청: {code}")
                except RuntimeError:
                    pass
        except Exception as ex:
            self.logger.error(f"❌ 실시간 체결 데이터 구독 실패: {code} - {ex}")
    
    def unsubscribe_realtime_execution_data(self, code):
        """실시간 체결 데이터 구독 해제"""
        try:
            ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
            if ws_client and ws_client.connected:
                try:
                    asyncio.get_running_loop()
                    create_fire_and_forget_task(ws_client.unsubscribe_stock_execution_data([code]))
                    self.logger.debug(f"📡 실시간 체결 데이터 구독 해제: {code}")
                except RuntimeError:
                    pass
        except Exception as ex:
            self.logger.error(f"❌ 실시간 체결 데이터 구독 해제 실패: {code} - {ex}")

    async def cleanup_stale_monitored_stocks(self):
        """설정된 TTL을 초과한 감시종목 자동 삭제 루프"""
        self.logger.info(f"🧹 감시종목 TTL({self.monitoring_ttl_minutes}분) 자동 정리 백그라운드 태스크 시작")
        while True:
            try:
                await asyncio.sleep(60) # 1분마다 체크
                
                if not self.monitored_stocks:
                    continue
                
                now = datetime.now()
                # 딕셔너리 크기가 변경될 수 있으므로 리스트로 복사하여 순회
                for code in list(self.monitored_stocks):
                    added_time = self.stock_added_time.get(code)
                    if added_time:
                        elapsed = (now - added_time).total_seconds() / 60.0
                        if elapsed > self.monitoring_ttl_minutes:
                            # 보유 여부 재확인
                            is_held = False
                            trader = getattr(self.parent, 'trader', None)
                            if trader and hasattr(trader, 'holdings'):
                                if code in trader.holdings and trader.holdings[code].get('quantity', 0) > 0:
                                    is_held = True
                            
                            if is_held:
                                # 보유 종목은 TTL 적용 제외, 편입 시간을 갱신하여 불필요한 로그 방지
                                self.stock_added_time[code] = now
                            else:
                                self.logger.info(f"⏳ TTL 만료({elapsed:.1f}분 경과): 종목 {code} 자동 삭제")
                                await self.remove_stock_from_monitoring(code)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"감시종목 TTL 정리 중 오류: {e}", exc_info=True)
                await asyncio.sleep(60)


# ==========================================
# 4. StrategyManager (전략 로드/저장 관리)
# ==========================================
class StrategyManager:
    """전략 로드/저장 관리 매니저 (Pure Python)"""
    
    def __init__(self, parent_app):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent_app
    
    def get_available_strategies(self):
        """가용한 전체 투자 전략 반환"""
        strategies = []
        try:
            config = EnvConfigParser()
            if config.has_section('STRATEGIES'):
                for key, value in config.items('STRATEGIES'):
                    if key.startswith('stg_') and key != 'stg_integrated':
                        strategies.append(value)
        except Exception as ex:
            self.logger.error(f"투자 전략 목록 로드 실패: {ex}")
        return strategies

    def get_strategies_by_type(self, current_strategy, strategy_type):
        """전략 타입('buy' 또는 'sell')에 따른 하위 전략 리스트 반환"""
        strategies = []
        try:
            config = EnvConfigParser()
            key_prefix = 'buy_stg_' if strategy_type == 'buy' else 'sell_stg_'
            
            if config.has_section(current_strategy):
                for key in config[current_strategy]:
                    if key.startswith(key_prefix):
                        try:
                            strategy_data = json.loads(config[current_strategy][key])
                            strategies.append({"key": key, "name": strategy_data.get('name', key)})
                        except json.JSONDecodeError:
                            self.logger.warning(f"전략 파싱 실패: {key}")
        except Exception as ex:
            self.logger.error(f"하위 전략 목록 로드 실패 ({strategy_type}): {ex}")
        return strategies

    def save_current_strategy(self, current_strategy):
        """현재 선택된 투자전략을 .env에 저장"""
        try:
            config = EnvConfigParser()
            if not config.has_section('SETTINGS'):
                config.add_section('SETTINGS')
            config.set('SETTINGS', 'last_strategy', current_strategy)
            config.save()
            
            self.parent.current_strategy = current_strategy
            self.logger.debug(f"✅ 현재 투자전략 저장(SETTINGS.last_strategy): {current_strategy}")
        except Exception as ex:
            self.logger.error(f"투자전략 저장 실패: {ex}")

    def get_strategy_content(self, current_strategy, strategy_name, strategy_type):
        """특정 전략 코드 콘텐츠 로드"""
        try:
            config = EnvConfigParser()
            target_section = current_strategy
            target_key = None
            display_name = strategy_name

            if not config.has_section(target_section):
                return ""

            for key, value in config.items(target_section):
                try:
                    strategy_data = ast.literal_eval(value)
                    if isinstance(strategy_data, dict) and strategy_data.get('name') == display_name:
                        if strategy_type == 'buy' and key.startswith('buy_stg_'):
                            target_key = key
                            break
                        elif strategy_type == 'sell' and key.startswith('sell_stg_'):
                            target_key = key
                            break
                except Exception:
                    continue

            if target_key:
                strategy_data = ast.literal_eval(config.get(target_section, target_key))
                return strategy_data.get('content', '')
        except Exception as ex:
            self.logger.error(f"전략 내용 로드 실패: {ex}")
        return ""

    def save_strategy_content(self, current_strategy, strategy_name, key_from_client, strategy_type, new_content):
        """전략 내용 저장 및 즉시 반영"""
        try:
            config = EnvConfigParser()
            target_section = current_strategy
            target_key = key_from_client

            if not config.has_section(target_section):
                self.logger.error(f"전략 저장 실패: 섹션을 찾을 수 없음 - [{target_section}]")
                return False

            if not config.has_option(target_section, target_key):
                self.logger.error(f"전략 저장 실패: 키를 찾을 수 없음 - {target_key}")
                return False

            try:
                strategy_json_str = config.get(target_section, target_key)
                strategy_data = ast.literal_eval(strategy_json_str)
                strategy_data['content'] = new_content
                config.set(target_section, target_key, json.dumps(strategy_data, ensure_ascii=False))
            except Exception as e:
                self.logger.error(f"전략 데이터 파싱 또는 수정 실패: {e}")
                return False

            config.save()
            self.logger.debug(f"{strategy_type} 전략 '{strategy_name}'이 저장되었습니다.")

            # 전략 즉시 반영
            if hasattr(self.parent, 'objstg') and self.parent.objstg:
                self.parent.objstg.load_strategy_config()
                self.logger.info(f"✅ {strategy_type} 전략이 즉시 반영되었습니다.")
            return True
        except Exception as ex:
            self.logger.error(f"전략 저장 실패: {ex}")
            return False

    async def _clear_monitoring_list(self):
        """모니터링 리스트 상태 초기화 (단, 보유 종목 제외)"""
        self.logger.info("🔄 투자 전략 변경으로 인해 기존 모니터링 목록을 초기화합니다.")
        
        # 보유 종목 추출
        holding_codes = set()
        trader = getattr(self.parent, 'trader', None)
        if trader and hasattr(trader, 'holdings'):
            for code, info in trader.holdings.items():
                if info.get('quantity', 0) > 0:
                    holding_codes.add(code)
        self.logger.debug(f"보유 종목은 모니터링에서 제외하지 않습니다: {list(holding_codes)}")

        # 모니터링 매니저 및 실시간 모니터링 종목 셋 가져오기
        monitoring_mgr = getattr(self.parent, 'monitoring_manager', None)
        monitored_stocks = monitoring_mgr.monitored_stocks if monitoring_mgr else set()

        # 해제할 구독 종목 (모니터링 중이었으나 보유 종목이 아닌 것들)
        codes_to_unsubscribe = list(monitored_stocks.difference(holding_codes))

        # 조건검색 해제
        if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
            all_seqs = [c['seq'] for c in self.parent.condition_search_list]
            for seq in all_seqs:
                await self.parent.stop_condition_realtime(seq)
        
        if hasattr(self.parent, 'active_realtime_conditions'):
            self.parent.active_realtime_conditions.clear()
        self.logger.debug("✅ 모든 실시간 조건검색 모니터링 중단 완료.")

        # 모니터링 종목 초기화 (보유 종목 제외) - 기존 set 레퍼런스를 유지하기 위해 intersection_update 사용
        if monitoring_mgr:
            monitoring_mgr.monitored_stocks.intersection_update(holding_codes)
            
            # TTL 시간 기록도 함께 정리
            keys_to_remove = [k for k in list(monitoring_mgr.stock_added_time.keys()) if k not in holding_codes]
            for k in keys_to_remove:
                monitoring_mgr.stock_added_time.pop(k, None)
        
        # 실시간 주식체결 데이터 구독 해제 전송 (UNREG)
        if codes_to_unsubscribe:
            ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
            if ws_client and ws_client.connected:
                from utils import create_fire_and_forget_task
                create_fire_and_forget_task(ws_client.unsubscribe_stock_execution_data(codes_to_unsubscribe))
                self.logger.info(f"🗑️ 전략 변경으로 인해 기존 {len(codes_to_unsubscribe)}개 종목 실시간 구독 해제 요청 완료")

        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
            self.parent.chart_cache.update_chart_update_interval()

        # 차트 캐시 정리
        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
            codes_to_remove = [code for code in self.parent.chart_cache.cache.keys() if code not in holding_codes]
            for code in codes_to_remove:
                self.parent.chart_cache.remove_monitoring_stock(code)
            self.logger.debug(f"차트 캐시에서 {len(codes_to_remove)}개 종목 제거 완료.")

    async def stg_changed(self, strategy_name=None):
        """전략 변경 이벤트 핸들러 (비동기)"""
        try:
            if not strategy_name:
                strategy_name = getattr(self.parent, 'current_strategy', None)
            
            if not strategy_name:
                return

            self.parent.condition_search_manager.is_manual_change = True
            self.logger.debug(f"투자 전략 변경: {strategy_name}")

            if getattr(self.parent, 'is_loading_strategy', False):
                self.logger.debug("초기화 중... 전략 저장을 건너뜁니다.")
                return
            
            await self._clear_monitoring_list()
            await asyncio.sleep(1.0)

            self.save_current_strategy(strategy_name)
            
            # 조건검색식 기동
            if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                condition_names = [condition['title'] for condition in self.parent.condition_search_list]
                if strategy_name in condition_names:
                    await self.parent.condition_search_manager.handle_condition_search(strategy_name)
            
        except Exception as ex:
            self.logger.error(f"전략 변경 실패: {ex}")


# ==========================================
# 5. TradingManager (매매 실행 관리)
# ==========================================
class TradingManager:
    """매매 실행 관리 매니저 (Pure Python)"""
    
    def __init__(self, parent_app):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent_app

    def get_target_buy_count(self):
        """.env에서 최대투자 종목수 읽기"""
        try:
            config = EnvConfigParser()
            if config.has_option('SETTINGS', 'buycount'):
                return config.getint('SETTINGS', 'buycount')
            else:
                return 3  # 기본값
        except Exception as ex:
            self.logger.error(f"buycount 읽기 실패: {ex}")
            return 3  # 기본값

    def buycount_setting(self, buycount):
        """투자 종목수 설정"""
        try:
            if buycount > 0:
                config = EnvConfigParser()
                if not config.has_section('SETTINGS'):
                    config.add_section('SETTINGS')
                
                config.set('SETTINGS', 'buycount', str(buycount))
                config.save()
                
                if hasattr(self.parent, 'trader') and self.parent.trader:
                    self.parent.trader.buycount = buycount
                
                self.logger.info(f"✅ 최대 투자 종목수 설정 완료: {buycount}종목")
                return True
            else:
                self.logger.warning("1 이상의 숫자를 입력해주세요.")
                return False
        except Exception as ex:
            self.logger.error(f"투자 종목수 설정 실패: {ex}")
            return False
    
    async def sell_all_item(self, is_auto=False):
        """전체 매도 (키움 REST API 기반) (비동기)
        
        Args:
            is_auto: True면 자동 청산, False면 수동 전체 매도
        """
        if self.parent.trading_lock.locked():
            self.logger.warning("⚠️ 다른 수동 매매 작업이 이미 진행 중입니다.")
            return False
            
        autotrader = getattr(self.parent, 'autotrader', None)
        chart_cache = getattr(self.parent, 'chart_cache', None)
        
        if autotrader:
            autotrader.stop_auto_trading()
        if chart_cache:
            chart_cache.stop()
        
        if is_auto:
            self.logger.info("자동 청산 - 모든 주기적 타이머 일시 중지")
        else:
            self.logger.info("수동 전체 매도 시작 - 모든 주기적 타이머 일시 중지")
            
        try:
            async with self.parent.trading_lock:
                trader = getattr(self.parent, 'trader', None)
                if not trader or not trader.holdings:
                    self.logger.warning("매도할 종목이 없습니다.")
                    await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                    return False
                
                sell_items = list(trader.holdings.keys())
                self.logger.info(f"🔄 전체 매도 대상 종목: {sell_items}")
                
                success_count = 0
                for code in sell_items:
                    try:
                        quantity = 0
                        
                        # 1차: 웹소켓 실시간 잔고 데이터에서 보유 수량 조회 시도
                        ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
                        if ws_client and hasattr(ws_client, 'balance_data'):
                            if code in ws_client.balance_data:
                                quantity = ws_client.balance_data[code].get('quantity', 0)
                                self.logger.debug(f"💰 웹소켓 잔고: {code} {quantity}주")
                        
                        # 2차: 웹소켓 데이터가 없거나 수량이 0이면 REST API로 조회
                        if quantity <= 0:
                            try:
                                if trader.client:
                                    balance_result = await trader.client.get_acnt_balance()
                                    if balance_result:
                                        holdings = balance_result.get('stk_acnt_evlt_prst', balance_result.get('output1', []))
                                        for stock in holdings:
                                            raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                                            stock_code = self.parent.data_manager.normalize_stock_code(raw_code)
                                            if stock_code == code:
                                                quantity = self.parent.data_manager.safe_int(stock.get('rmnd_qty', stock.get('hldg_qty', 0)))
                                                self.logger.debug(f"📡 REST API 잔고: {code} {quantity}주")
                                                break
                            except Exception as api_ex:
                                self.logger.error(f"❌ REST API 잔고 조회 실패: {api_ex}")
                        
                        if quantity <= 0:
                            self.logger.warning(f"⚠️ {code} 보유 수량 없음 - 건너뜀")
                            continue

                        if trader.pending_sell_orders and code in trader.pending_sell_orders:
                            self.logger.info(f"⏳ {code} 이미 매도 주문이 진행 중이므로 건너뜁니다.")
                            continue
                        
                        if trader.client:
                            max_retries = 3
                            retry_delay = 1.0
                            success = False
                            
                            for attempt in range(max_retries):
                                try:
                                    success = await trader.client.place_sell_order(code, quantity, 0, "market")
                                    if success:
                                        success_count += 1
                                        self.logger.info(f"✅ 매도 성공: {code} {quantity}주")
                                        break
                                    else:
                                        self.logger.warning(f"⚠️ 매도 주문 실패 (시도 {attempt + 1}/{max_retries}): {code}")
                                except Exception as order_ex:
                                    error_msg = str(order_ex)
                                    if "429" in error_msg or "Too Many Requests" in error_msg:
                                        if attempt < max_retries - 1:
                                            self.logger.warning(f"⚠️ API Rate Limit 초과 - {retry_delay}초 후 재시도 ({attempt + 1}/{max_retries}): {code}")
                                            await asyncio.sleep(retry_delay)
                                            retry_delay *= 2
                                            continue
                                        else:
                                            self.logger.error(f"❌ API Rate Limit 초과 - 최대 재시도 횟수 도달: {code}")
                                    else:
                                        self.logger.error(f"❌ 매도 주문 오류: {code} - {error_msg}")
                                        break
                            
                            if len(sell_items) > 1:
                                await asyncio.sleep(0.8)
                                
                    except Exception as item_ex:
                        self.logger.error(f"❌ {code} 매도 중 오류: {item_ex}")
                
                if success_count > 0:
                    self.logger.info(f"✅ 전체 매도 완료: {success_count}개 종목 매도")
                    return True
                else:
                    self.logger.error("❌ 전체 매도 실패")
                    return False
        except Exception as ex:
            self.logger.error(f"전체 매도 작업 중 오류 발생: {ex}", exc_info=True)
            return False
        finally:
            await self._restart_timers_after_manual_trade(autotrader, chart_cache)
    
    async def sell_item(self, code, quantity=None):
        """특정 종목 매도 (비동기)"""
        if self.parent.trading_lock.locked():
            self.logger.warning("⚠️ 다른 수동 매매 작업이 이미 진행 중입니다.")
            return False
            
        autotrader = getattr(self.parent, 'autotrader', None)
        chart_cache = getattr(self.parent, 'chart_cache', None)
        
        if autotrader:
            autotrader.stop_auto_trading()
        if chart_cache:
            chart_cache.stop()
            
        self.logger.debug(f"수동 매도 시작 - {code}")
        try:
            async with self.parent.trading_lock:
                trader = getattr(self.parent, 'trader', None)
                if not trader:
                    self.logger.error("트레이더가 초기화되지 않았습니다")
                    return False

                # 수량이 주어지지 않은 경우 전량 매도
                if quantity is None or quantity <= 0:
                    quantity = 0
                    
                    # 1차: REST API로 보유수량 조회
                    try:
                        if trader.client:
                            balance_result = await trader.client.get_acnt_balance()
                            if balance_result:
                                holdings = balance_result.get('stk_acnt_evlt_prst', balance_result.get('output1', []))
                                for stock in holdings:
                                    raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                                    stock_code = self.parent.data_manager.normalize_stock_code(raw_code)
                                    if stock_code == code:
                                        quantity = self.parent.data_manager.safe_int(stock.get('rmnd_qty', 0))
                                        self.logger.debug(f"✅ REST API로 주문가능수량 조회 성공: {code} {quantity}주")
                                        break
                    except Exception as api_ex:
                        self.logger.error(f"❌ REST API 잔고 조회 실패: {api_ex}")

                    # 2차: Fallback 웹소켓 잔고 조회
                    if quantity <= 0:
                        ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
                        if ws_client and hasattr(ws_client, 'balance_data'):
                            if code in ws_client.balance_data:
                                quantity = ws_client.balance_data[code].get('order_available_qty', 0)
                                self.logger.info(f"💰 웹소켓 잔고 조회 (Fallback): {code} 주문가능수량 {quantity}주")
                
                if quantity <= 0:
                    self.logger.warning(f"⚠️ 보유 수량 없음: {code}")
                    return False
                
                if trader.client:
                    success = await trader.client.place_sell_order(code, quantity, 0, "market")
                    if success:
                        self.logger.debug(f"✅ 매도 주문 성공: {code} {quantity}주 매도")
                        return True
                    else:
                        self.logger.error(f"❌ 매도 주문 실패: {code}")
                        return False
                else:
                    self.logger.error("키움 클라이언트가 초기화되지 않았습니다")
                    return False
        except Exception as ex:
            self.logger.error(f"매도 작업 중 오류 발생: {ex}", exc_info=True)
            return False
        finally:
            await self._restart_timers_after_manual_trade(autotrader, chart_cache)
    
    async def buy_item(self, code, quantity=None):
        """종목 매입 - 자동 매입가능수량 계산 또는 지정 수량 매수 (비동기)"""
        if self.parent.trading_lock.locked():
            self.logger.warning("⚠️ 다른 수동 매매 작업이 이미 진행 중입니다.")
            return False
            
        autotrader = getattr(self.parent, 'autotrader', None)
        chart_cache = getattr(self.parent, 'chart_cache', None)
        
        if autotrader:
            autotrader.stop_auto_trading()
        if chart_cache:
            chart_cache.stop()
            
        self.logger.debug(f"수동 매수 시작 - {code}")
        try:
            async with self.parent.trading_lock:
                trader = getattr(self.parent, 'trader', None)
                if not trader:
                    self.logger.error("⚠️ trader가 초기화되지 않았습니다 (API 연결이 필요합니다)")
                    return False

                # 이미 보유 중인 종목인지 검증
                if code in trader.holdings and trader.holdings[code].get('quantity', 0) > 0:
                    self.logger.info(f"⚠️ 매수 주문 취소: {code}는 이미 보유 중인 종목입니다.")
                    return False
                
                if quantity is None or quantity <= 0:
                    # 자동 수량 계산
                    available_cash = await trader.get_available_cash()
                    if available_cash <= 0:
                        self.logger.warning(f"⚠️ 매수 주문 취소: 투자가능금액 부족 ({available_cash:,.0f}원)")
                        return False
                    
                    available_buy_count = self.parent.login_handler.get_available_buy_count()
                    if available_buy_count <= 0:
                        self.logger.warning(f"⚠️ 매수 주문 취소: 최대 보유 종목 수 도달")
                        return False
                    
                    current_price = 0
                    price_source = ""
                    
                    # 1순위: 캐시에서 현재가 조회
                    if chart_cache:
                        cached_data = chart_cache.get_cached_data(code)
                        if cached_data and cached_data.get('tic_data'):
                            tic_data = cached_data['tic_data']
                            if tic_data.get('close') and len(tic_data['close']) > 0:
                                current_price = float(tic_data['close'][-1])
                                price_source = "캐시"
                    
                    # 2순위: REST API 현재가 조회
                    if current_price <= 0:
                        try:
                            current_price = await trader.get_current_price(code)
                            if current_price > 0: price_source = "API"
                        except Exception as price_ex:
                            self.logger.debug(f"현재가 조회 실패: {price_ex}")

                    if current_price <= 0:
                        self.logger.error(f"❌ 현재가 조회에 실패하여 매수 주문을 취소합니다: {code}")
                        return False
                    
                    budget_per_stock = available_cash // available_buy_count
                    quantity = int(budget_per_stock / current_price)
                    if quantity <= 0: quantity = 1
                    
                    self.logger.debug(f"🛒 {code} 매수: {quantity}주 @ 시장가 (예산 {budget_per_stock:,.0f}원, 현재가 {current_price:,.0f}원/{price_source})")
                
                if trader.client:
                    success = await trader.client.place_buy_order(code, quantity, 0, "market")
                    if success:
                        self.logger.debug(f"✅ 매수 주문 성공: {code} {quantity}주 매수")
                        return True
                    else:
                        self.logger.error(f"❌ 매수 주문 실패: {code}")
                        return False
                else:
                    self.logger.error("키움 클라이언트가 초기화되지 않았습니다")
                    return False
        except Exception as ex:
            self.logger.error(f"매입 작업 중 오류 발생: {ex}", exc_info=True)
            return False
        finally:
            await self._restart_timers_after_manual_trade(autotrader, chart_cache)
    
    async def _restart_timers_after_manual_trade(self, autotrader, chart_cache):
        """수동 매매 후 타이머들을 재시작하는 헬퍼 함수"""
        await asyncio.sleep(1)
        if autotrader:
            autotrader.start_auto_trading()
        if chart_cache:
            chart_cache.start()
        self.logger.debug("수동 매매 완료 - 모든 주기적 타이머 다시 시작")


# ==========================================
# 6. AccountManager (계좌 잔고 및 관리)
# ==========================================
class AccountManager:
    """계좌 조회 및 잔고 관리 매니저 (Pure Python)"""
    
    def __init__(self, parent_app):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent_app
    
    async def handle_acnt_balance_query(self):
        """계좌 잔고조회 - REST API로 초기 조회 후 실시간 업데이트 (비동기)
        
        1. REST API로 초기 잔고 조회 및 보유종목 리스트 생성
        2. 웹소켓 실시간 잔고(04)로 변동사항 추적
        """
        parent = self.parent
        try:
            self.logger.debug("🔧 계좌 잔고 조회 시작 (REST API)")
            
            if not hasattr(parent, 'trader') or not parent.trader:
                self.logger.warning("⚠️ 트레이더가 초기화되지 않았습니다")
                return
            
            if not hasattr(parent.trader, 'client') or not parent.trader.client:
                self.logger.warning("⚠️ API 클라이언트가 연결되지 않았습니다")
                return

            # 1. 예수금상세현황 조회 (kt00001)
            self.logger.debug("🔍 예수금상세현황 조회 중...")
            try:
                deposit_data = await parent.trader.client.get_deposit_detail()
                if deposit_data:
                    self.logger.debug("✅ 예수금상세현황 조회 성공")
                    # 투자원금(entr) 캐싱
                    prime_cash = parent.data_manager.safe_int(deposit_data.get('entr', 0))
                    if prime_cash > 0:
                        parent.trader.prime_cash = prime_cash
                    
                    # 'ord_alow_amt' (주문가능금액)을 우선 예수금으로 활용하고, 없으면 'entr' 사용
                    entr_amount = parent.data_manager.safe_int(deposit_data.get('ord_alow_amt', deposit_data.get('entr', 0)))
                    parent.trader._cash_cache = entr_amount
                    
                    ws_client = getattr(parent.login_handler, 'websocket_client', None)
                    if ws_client:
                        if not hasattr(ws_client, 'balance_data') or ws_client.balance_data is None:
                            ws_client.balance_data = {}
                        ws_client.balance_data['available_cash'] = entr_amount
                else:
                    self.logger.warning("⚠️ 예수금상세현황 조회 실패")
            except Exception as deposit_ex:
                self.logger.error(f"❌ 예수금상세현황 조회 실패: {deposit_ex}")

            # 2. REST API 잔고조회 (kt00004) - 초기 보유종목 확인
            self.logger.debug("🔍 계좌 잔고 조회 중...")
            try:
                balance_data = await parent.trader.client.get_acnt_balance()
                if balance_data:
                    holdings = balance_data.get('stk_acnt_evlt_prst', balance_data.get('output1', []))
                    if holdings and len(holdings) > 0:
                        self.logger.info(f"📦 보유 종목 수: {len(holdings)}개")
                        await self._initialize_balance_data_from_rest_api(holdings)
                    else:
                        self.logger.info("📦 현재 보유 종목이 없습니다.")
                else:
                    self.logger.warning("⚠️ 계좌 잔고 조회 실패 또는 보유종목 없음")
            except Exception as balance_ex:
                self.logger.error(f"계좌 잔고 조회 실패: {balance_ex}", exc_info=True)
        except Exception as ex:
            self.logger.error(f"계좌 잔고 조회 실패: {ex}", exc_info=True)
            
    async def handle_acnt_balance_query_async(self):
        """계좌 잔고조회 (비동기 버전) - 초기화 단계에서 사용"""
        try:
            self.logger.debug("🔧 계좌 잔고 조회 시작 (비동기)")
            if not hasattr(self.parent, 'trader') or not self.parent.trader:
                self.logger.warning("⚠️ 트레이더가 초기화되지 않았습니다")
                return
            
            # 1. 예수금상세현황 조회
            try:
                deposit_data = await self.parent.trader.client.get_deposit_detail()
                if deposit_data:
                    # 투자원금(entr) 캐싱
                    prime_cash = self.parent.data_manager.safe_int(deposit_data.get('entr', 0))
                    if hasattr(self.parent, 'trader') and self.parent.trader and prime_cash > 0:
                        self.parent.trader.prime_cash = prime_cash
                    
                    # 'ord_alow_amt' (주문가능금액)을 우선 예수금으로 활용하고, 없으면 'entr' 사용
                    entr_amount = self.parent.data_manager.safe_int(deposit_data.get('ord_alow_amt', deposit_data.get('entr', 0)))
                    self.logger.info(f"예수금: {entr_amount:,}원")
                    
                    if hasattr(self.parent, 'trader') and self.parent.trader:
                        self.parent.trader._cash_cache = entr_amount
                        ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
                        if ws_client:
                            if not hasattr(ws_client, 'balance_data') or ws_client.balance_data is None:
                                ws_client.balance_data = {}
                            ws_client.balance_data['available_cash'] = entr_amount
            except Exception as deposit_ex:
                self.logger.error(f"❌ 예수금상세현황 조회 실패: {deposit_ex}")

            # 2. 계좌평가잔고내역 조회 (kt00018)
            try:
                self.logger.debug("🔍 계좌평가잔고내역 조회 중...")
                eval_status = await self.parent.trader.client.get_account_evaluation_status()
                if eval_status:
                    total_purchase_amount = self.parent.data_manager.safe_int(eval_status.get('tot_pur_amt', 0))
                    total_eval_pl_amount = self.parent.data_manager.safe_int(eval_status.get('tot_evlt_pl', 0))
                    total_profit_rate = self.parent.data_manager.safe_float(eval_status.get('tot_prft_rt', 0.0))

                    self.logger.info(f"보유종목 총매입금액: {total_purchase_amount:,}원")
                    self.logger.info(f"보유종목 총평가손익: {total_eval_pl_amount:+,}원")
                    self.logger.info(f"보유종목 총수익률: {total_profit_rate:.2f}%")
                    
                    prime_cash = getattr(self.parent.trader, 'prime_cash', 0)
                    available_cash = getattr(self.parent.trader, '_cash_cache', 0)
                    if prime_cash > 0:
                        total_assets = available_cash + total_purchase_amount + total_eval_pl_amount
                        account_profit_rate = ((total_assets - prime_cash) / prime_cash) * 100
                        self.logger.info(f"계좌 총수익률: {account_profit_rate:.2f}% (총자산: {total_assets:,}원)")
                else:
                    self.logger.warning("⚠️ 계좌평가잔고내역 조회 실패")
            except Exception as eval_ex:
                self.logger.error(f"❌ 계좌평가잔고내역 조회 중 오류: {eval_ex}", exc_info=True)

            # 3. REST API 잔고조회
            try:
                balance_data = await self.parent.trader.client.get_acnt_balance()
                if balance_data and 'stk_acnt_evlt_prst' in balance_data:
                    holdings = balance_data.get('stk_acnt_evlt_prst', [])
                    await self._initialize_balance_data_from_rest_api(holdings)
                else:
                    self.logger.warning("⚠️ 계좌 잔고 조회 실패 또는 보유 종목 없음 (비동기 조회)")
            except Exception as balance_ex:
                self.logger.error(f"계좌 잔고 조회 실패 (비동기): {balance_ex}", exc_info=True)
        except Exception as ex:
            self.logger.error(f"계좌 잔고 조회 실패 (비동기): {ex}", exc_info=True)

    async def _initialize_balance_data_from_rest_api(self, holdings):
        """REST API 잔고 데이터를 웹소켓 balance_data 형식으로 변환"""
        parent = self.parent
        try:
            self.logger.debug("🔧 REST API 잔고 데이터를 웹소켓 balance_data 형식으로 변환 중...")
            ws_client = getattr(parent.login_handler, 'websocket_client', None)
            if not ws_client:
                self.logger.warning("⚠️ websocket_client가 없습니다 - 데이터를 로드할 수 없습니다")
                return
            
            converted_count = 0
            for stock in holdings:
                stock_code = '알수없음'
                try:
                    raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                    stock_code = parent.data_manager.normalize_stock_code(raw_code)
                    
                    if hasattr(parent, 'trader') and parent.trader and parent.trader.is_recently_sold(stock_code):
                        self.logger.debug(f"🚫 {stock_code} 최근 매도된 종목이므로 REST API 잔고 반영 건너뜀")
                        continue

                    stock_name = stock.get('stk_nm', stock.get('prdt_name', ''))
                    quantity = parent.data_manager.safe_int(stock.get('rmnd_qty', stock.get('hldg_qty', 0)))
                    current_price = parent.data_manager.safe_int(stock.get('cur_prc', stock.get('prpr', 0)))
                    average_price = parent.data_manager.safe_int(stock.get('avg_prc', stock.get('pchs_avg_pric', 0)))
                    
                    if stock_code and quantity > 0:
                        commission_rate = parent.trader.commission_rate
                        tax_rate = parent.trader.tax_rate

                        buy_cost_per_share = average_price * (1 + commission_rate)
                        sell_revenue_per_share = current_price * (1 - commission_rate - tax_rate)
                        net_profit_per_share = sell_revenue_per_share - buy_cost_per_share

                        profit_loss = net_profit_per_share * quantity
                        total_buy_cost = buy_cost_per_share * quantity
                        profit_rate = (profit_loss / total_buy_cost) * 100 if total_buy_cost > 0 else 0.0

                        evaluation_amount = quantity * current_price
                        purchase_amount = quantity * average_price
                        
                        ws_client.balance_data[stock_code] = {
                            'code': stock_code,
                            'name': stock_name,
                            'quantity': quantity,
                            'average_price': average_price,
                            'current_price': current_price,
                            'evaluation_amount': evaluation_amount,
                            'purchase_amount': purchase_amount,
                            'profit_loss': profit_loss,
                            'profit_loss_rate': profit_rate,
                            'order_available_qty': quantity,
                            'total_purchase': purchase_amount,
                            'daily_net_buy': 0,
                            'daily_total_profit': 0,
                            'daily_realized_profit': 0,
                            'daily_realized_profit_rate': 0,
                            'updated_at': datetime.now().isoformat()
                        }
                        
                        if hasattr(parent, 'trader') and parent.trader:
                            if stock_code not in parent.trader.holdings:
                                parent.trader.holdings[stock_code] = {'quantity': quantity}
                                if stock_code not in parent.trader.buy_prices:
                                    parent.trader.buy_prices[stock_code] = average_price
                                if stock_code not in parent.trader.buy_times:
                                    parent.trader.buy_times[stock_code] = datetime.now()
                            else:
                                parent.trader.holdings[stock_code]['quantity'] = quantity
                                if stock_code not in parent.trader.buy_prices or parent.trader.buy_prices[stock_code] == 0:
                                    parent.trader.buy_prices[stock_code] = average_price
                        
                        if hasattr(parent, 'monitoring_manager'):
                            await parent.monitoring_manager.add_stock_to_monitoring_async(stock_code)
                        
                        converted_count += 1
                except Exception as item_ex:
                    self.logger.error(f"❌ 종목 데이터 변환 실패 ({stock_code}): {item_ex}")
                    continue
            
            self.logger.info(f"✅ REST API 잔고 데이터 변환 완료: {converted_count}개 종목")
        except Exception as ex:
            self.logger.error(f"❌ REST API 잔고 데이터 변환 실패: {ex}", exc_info=True)


# ==========================================
# 7. ConditionSearchManager (조건검색 관리)
# ==========================================
class ConditionSearchManager:
    """조건검색 관리 매니저 (Pure Python)"""
    
    def __init__(self, parent_app):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent_app
        self.is_manual_change = False  # 사용자의 수동 변경 여부 플래그

    async def handle_condition_search_list_query(self):
        """조건검색 목록조회 (웹소켓 기반)"""
        try:
            self.logger.debug("🔍 조건검색 목록조회 시작 (웹소켓)")

            if not hasattr(self.parent, 'trader') or not self.parent.trader:
                self.logger.warning("⚠️ 트레이더가 초기화되지 않았습니다")
                return

            if not hasattr(self.parent.trader, 'client') or not self.parent.trader.client:
                self.logger.warning("⚠️ API 클라이언트가 연결되지 않았습니다")
                return

            # 웹소켓 클라이언트 확인
            ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
            if not ws_client:
                self.logger.warning("⚠️ 웹소켓 클라이언트가 연결되지 않았습니다")
                return

            # 웹소켓을 통한 조건검색 목록조회
            try:
                await ws_client.send_message({
                    'trnm': 'CNSRLST',  # TR명
                })
                logging.debug("✅ 조건검색 목록조회 요청 전송 완료 (웹소켓)")
            except Exception as websocket_ex:
                self.logger.error(f"❌ 조건검색 목록조회 웹소켓 요청 실패: {websocket_ex}")
                self.parent.condition_search_list = None
        except Exception as ex:
            self.logger.error(f"❌ 조건검색 목록조회 실패: {ex}")
            self.parent.condition_search_list = None
  
    async def check_and_auto_execute_saved_condition(self):
        """저장된 조건검색식이 있는지 확인하고 자동 실행"""
        if self.is_manual_change:
            self.logger.debug("사용자 수동 변경으로 인해 저장된 조건검색식 자동 실행을 건너뜁니다.")
            self.is_manual_change = False  # 플래그 초기화
            return False

        try:
            # .env에서 저장된 전략 확인
            config = EnvConfigParser()
            
            if config.has_option('SETTINGS', 'last_strategy'):
                last_strategy = config.get('SETTINGS', 'last_strategy')
                self.logger.debug(f"📋 저장된 전략 확인: {last_strategy}")
                
                # 전역 현재 전략 설정
                self.parent.current_strategy = last_strategy

                # 저장된 전략이 조건검색식인지 확인 (조건검색 목록에 있는지 확인)
                if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                    condition_names = [condition['title'] for condition in self.parent.condition_search_list]
                    if last_strategy in condition_names:
                        self.logger.info(f"🔍 저장된 조건검색식 발견: {last_strategy}")
                        
                        # 자동 실행 (1초 후)
                        async def delayed_condition_search():
                            await asyncio.sleep(1.0)  # 1초 대기
                            await self.parent.strategy_manager.stg_changed()

                        create_fire_and_forget_task(delayed_condition_search())
                        self.logger.debug("🔍 저장된 조건검색식 자동 실행 예약 (1초 후)")
                        return True
                # 일반 조건검색식인 경우
                if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                    condition_names = [condition['title'] for condition in self.parent.condition_search_list]
                    if last_strategy in condition_names:
                        self.logger.debug(f"🔍 저장된 조건검색식 발견: {last_strategy}")
                        async def delayed_search():
                            await asyncio.sleep(1.5)
                            await self.handle_condition_search(last_strategy)
                        create_fire_and_forget_task(delayed_search())
                        self.logger.debug("🔍 저장된 조건검색식 자동 실행 예약 (1.5초 후)")
                        return True
                else:
                    self.logger.debug(f"📋 저장된 전략이 조건검색식이 아닙니다: {last_strategy}")
                    return False
            else:
                self.logger.debug("📋 저장된 전략이 없습니다")
                return False
            
        except Exception as ex:
            self.logger.error(f"❌ 저장된 조건검색식 확인 및 자동 실행 실패: {ex}", exc_info=True)
            return False
    
    async def handle_condition_search(self, condition_name=None):
        """조건검색 실행 (웹소켓 기반)"""
        try:
            if not condition_name:
                condition_name = getattr(self.parent, 'current_strategy', None)
                
            if condition_name and hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                condition_seq = next((item['seq'] for item in self.parent.condition_search_list if item['title'] == condition_name), None)
                if condition_seq is not None:
                    self.logger.info(f"🔍 조건검색 시작: {condition_name} (seq: {condition_seq})")
                    await self.parent.execute_condition_search(condition_seq, condition_name)
        except Exception as ex:
            self.logger.error(f"조건검색 실행 실패: {ex}", exc_info=True)

    async def stop_all_conditions(self):
        """모든 실시간 조건검색 중단"""
        try:
            if not hasattr(self.parent, 'condition_search_list') or not self.parent.condition_search_list:
                return

            self.logger.info("🛑 모든 실시간 조건검색 중단 요청")
            for condition in self.parent.condition_search_list:
                seq = condition.get('seq')
                if seq is not None:
                    await self.parent.stop_condition_realtime(seq)
                    await asyncio.sleep(0.2)  # 약간의 딜레이
            
            self.logger.info("✅ 모든 실시간 조건검색이 중단되었습니다.")
        except Exception as ex:
            self.logger.error(f"❌ 모든 실시간 조건검색 중단 실패: {ex}")


# ==========================================
# 8. MLManager (머신러닝 모델 관리)
# ==========================================
class MLManager:
    """머신러닝 학습 관리자"""
    
    def __init__(self, parent):
        self.parent = parent
        self.logger = logging.getLogger(self.__class__.__name__)
        self.trainer_thread = None
        self.scheduler_task = None
        
        # 비동기 학습 스케줄러 루프 시작
        self.scheduler_task = create_fire_and_forget_task(self._scheduler_loop())
        
        self.logger.info("🤖 ML 매니저 초기화 완료 (asyncio 스케줄러 동작 중)")
        
    async def _scheduler_loop(self):
        """1분마다 스케줄을 체크하는 비동기 루프"""
        while True:
            try:
                self._check_schedule()
            except Exception as ex:
                self.logger.error(f"❌ ML 스케줄 체크 중 오류: {ex}")
            await asyncio.sleep(60)  # 1분 대기
            
    def _check_schedule(self):
        """정해진 시간에 학습 시작"""
        now = datetime.now()
        current_time_str = now.strftime('%H:%M')
        
        # 1. 점심시간 학습 (12:30)
        if current_time_str == '12:30':
            self.logger.info("⏰ 점심시간 도래: AI 모델 중간 학습(Light Update)을 시작합니다.")
            self.start_training()
            
        # 2. 장 마감 후 학습 (15:30)
        if current_time_str == '15:30':
            self.logger.info("⏰ 장 마감: AI 모델 정밀 학습(Deep Training)을 시작합니다.")
            self.start_training()
            
    def start_training(self):
        """학습 스레드 시작"""
        try:
            if self.trainer_thread is not None and self.trainer_thread.is_alive():
                self.logger.warning("⚠️ 이미 학습이 진행 중입니다.")
                return

            self.logger.info("🚀 ML 학습 스레드 시작 요청...")
            
            # 콜백을 전달하여 워커 스레드 생성 (threading.Thread 기반)
            self.trainer_thread = MLTrainingWorker(
                on_progress=self._on_progress,
                on_finished=self._on_finished
            )
            
            # 시작
            self.trainer_thread.start()
            
        except Exception as ex:
            self.logger.error(f"❌ ML 학습 시작 실패: {ex}")
            
    def _on_progress(self, msg):
        """학습 진행 상황 로그 출력"""
        self.logger.info(f"{msg}")

    def _on_finished(self, success, msg):
        """학습 완료 처리"""
        if success:
            self.logger.info(f"✨ {msg}")
        else:
            self.logger.warning(f"⚠️ {msg}")
        
        # 참조 제거
        self.trainer_thread = None

    def stop(self):
        """리소스 정리"""
        if self.scheduler_task:
            self.scheduler_task.cancel()
            self.logger.info("⏹️ ML 매니저 스케줄러 루프 중지됨")


__all__ = [
    'LoginHandler',
    'DataManager',
    'MonitoringManager',
    'StrategyManager',
    'TradingManager',
    'AccountManager',
    'ConditionSearchManager',
    'MLManager',
]
