import logging
import asyncio
import io

from config_manager import EnvConfigParser
import aiofiles
from datetime import datetime, timedelta
from PyQt6.QtCore import QObject, pyqtSignal
from utils import get_resource_path
from kiwoom_api import KiwoomRestClient, KiwoomWebSocketClient
from trader import KiwoomTrader
from strategy import KiwoomStrategy


class LoginHandler(QObject):
    """로그인 및 연결 관리 클래스"""
    
    # 시그널 정의: 연결 상태가 변경될 때 UI 업데이트를 위해 사용
    connection_status_changed = pyqtSignal(bool)
    
    def __init__(self, parent_window):
        super().__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent_window
        self.config = EnvConfigParser()
        self.kiwoom_client = None
    

    def get_current_holdings_count(self):
        """현재 보유종목 수 조회"""
        try:
            # 1차: 보유종목 리스트박스(boughtBox)에서 직접 확인 (가장 정확함)
            if hasattr(self.parent, 'boughtBox') and self.parent.boughtBox:
                count = self.parent.boughtBox.count()
                self.logger.debug(f"📊 보유종목 수 (boughtBox): {count}개")
                return count
            
            # 2차: KiwoomTrader의 holdings 확인
            if hasattr(self.parent, 'trader') and self.parent.trader:
                if hasattr(self.parent.trader, 'holdings'):
                    count = len(self.parent.trader.holdings)
                    self.logger.debug(f"📊 보유종목 수 (trader.holdings): {count}개")
                    return count
            
            # 3차: 웹소켓 실시간 잔고 데이터 확인 (변동이 있을 때만 업데이트됨)
            balance_data = self.parent.trader.get_balance_data()
            holdings = balance_data.get('holdings', {})
            # 수량이 0보다 큰 종목만 카운트
            active_holdings = {code: info for code, info in holdings.items() 
                             if info.get('quantity', 0) > 0}
            count = len(active_holdings) # type: ignore
            self.logger.debug(f"📊 보유종목 수 (balance_data): {count}개")
            return count
            
        except Exception as ex:
            self.logger.error(f"보유종목 수 조회 실패: {ex}", exc_info=True)
            return 0
    
    def get_available_buy_count(self):
        """매수가능 종목수 계산 (최대투자종목수 - 현재보유종목수)"""
        try:
            max_count = self.parent.trading_manager.get_target_buy_count() # type: ignore
            current_count = self.get_current_holdings_count()
            available_count = max(0, max_count - current_count)
            
            logging.debug(f"📊 투자 종목 현황: 최대 {max_count}종목, 현재 보유 {current_count}종목, 매수가능 {available_count}종목")
            return available_count
        except Exception as ex:
            self.logger.error(f"매수가능 종목수 계산 실패: {ex}", exc_info=True)
            return 1  # 기본값 # type: ignore
    
    def load_settings_sync(self):
        """설정 로드 (동기 I/O)"""
        try:
            if self.config.has_option('KIWOOM_API', 'simulation'): # type: ignore
                is_simulation = self.config.getboolean('KIWOOM_API', 'simulation')
                self.parent.trading_tab.tradingModeCombo.setCurrentIndex(0 if is_simulation else 1)
            if self.config.has_option('LOGIN', 'autoconnect'):
                self.parent.trading_tab.autoConnectCheckBox.setChecked(self.config.getboolean('LOGIN', 'autoconnect'))
        except Exception as ex:
            self.logger.error(f"설정 로드 폴백 실패: {ex}", exc_info=True)
    
    async def save_settings(self):
        """설정 저장 (비동기 I/O)"""
        try:
            # 설정 저장 전, 파일의 최신 내용을 다시 읽어와 동기화
            settings_path = get_resource_path('.env')
            self.logger.debug("설정 저장을 위해 .env 파일 다시 로드")
            
            # 거래 모드 설정 저장
            is_simulation = (self.parent.trading_tab.tradingModeCombo.currentIndex() == 0) # type: ignore
            self.config.set('KIWOOM_API', 'simulation', str(is_simulation))
            
            # 자동 연결 설정 저장
            self.config.set('LOGIN', 'autoconnect', str(self.parent.trading_tab.autoConnectCheckBox.isChecked()))
            
            # 현재 선택된 투자전략 저장
            current_strategy = self.parent.trading_tab.comboStg.currentText()
            if current_strategy:
                self.config.set('SETTINGS', 'last_strategy', current_strategy)

            # 비동기 파일 쓰기
            config_string = self._config_to_string()
            async with aiofiles.open(settings_path, 'w', encoding='utf-8') as f:
                await f.write(config_string)
                
        except Exception as ex:
            self.logger.error(f"설정 저장 실패: {ex}", exc_info=True)
            # 동기 방식으로 폴백
            self.save_settings_sync()
    
    def save_settings_sync(self):
        """설정 저장 (동기 I/O)"""
        try:
            settings_path = get_resource_path('.env')
            # 거래 모드 설정 저장
            is_simulation = (self.parent.trading_tab.tradingModeCombo.currentIndex() == 0) # type: ignore
            self.config.set('KIWOOM_API', 'simulation', str(is_simulation))
            
            # 자동 연결 설정 저장
            self.config.set('LOGIN', 'autoconnect', str(self.parent.trading_tab.autoConnectCheckBox.isChecked()))
            
            # 현재 선택된 투자전략 저장
            current_strategy = self.parent.trading_tab.comboStg.currentText()
            if current_strategy:
                self.config.set('SETTINGS', 'last_strategy', current_strategy)

            # 동기 파일 쓰기
            with open(settings_path, 'w', encoding='utf-8') as configfile:
                self.config.write(configfile)
        except Exception as ex:
            self.logger.error(f"설정 저장 폴백 실패: {ex}", exc_info=True)
    
    def _config_to_string(self):
        """ConfigParser를 문자열로 변환"""
        string_io = io.StringIO()
        self.config.write(string_io)
        return string_io.getvalue()
    
    async def start_websocket_client(self):
        """웹소켓 클라이언트 시작 (qasync 방식)"""
        try:           
            # kiwoom_client가 None인지 확인
            if self.kiwoom_client is None:
                self.logger.error("❌ 키움 클라이언트가 초기화되지 않았습니다. 먼저 API 연결을 시도해주세요.")
                return
            
            # 이미 연결된 웹소켓 클라이언트가 있으면 재사용 (balance_data 보존)
            if hasattr(self, 'websocket_client') and self.websocket_client and self.websocket_client.connected:
                self.logger.info("✅ 웹소켓 클라이언트가 이미 연결되어 있습니다 (재사용)")
                return
            
            # 기존 balance_data 백업 (웹소켓 재생성 시 데이터 보존)
            existing_balance_data = {}
            if hasattr(self, 'websocket_client') and self.websocket_client and hasattr(self.websocket_client, 'balance_data'):
                existing_balance_data = dict(self.websocket_client.balance_data)
                if existing_balance_data: # type: ignore
                    self.logger.info(f"💾 기존 웹소켓 balance_data 백업: {list(existing_balance_data.keys())} ({len(existing_balance_data)}개 종목)")
            
            # 웹소켓 클라이언트 초기화
            token = self.kiwoom_client.access_token
            is_mock = self.kiwoom_client.is_mock
            logger = logging.getLogger('KiwoomWebSocketClient') # type: ignore
            
            # 웹소켓 클라이언트 초기화 로그 제거
            self.websocket_client = KiwoomWebSocketClient(token, logger, is_mock, self.parent)
            
            # 백업한 balance_data 복원
            if existing_balance_data:
                self.websocket_client.balance_data = existing_balance_data
                self.logger.info(f"✅ 웹소켓 balance_data 복원 완료: {list(self.websocket_client.balance_data.keys())} ({len(self.websocket_client.balance_data)}개 종목)")
            
            # 웹소켓 서버에 먼저 연결한 후 실행 (메인 스레드에서 qasync 사용)
            # 연결 시도 로그 제거
            
            # 메인 스레드에서 qasync로 웹소켓 실행
            
            # 웹소켓 클라이언트를 비동기 태스크로 실행 # type: ignore
            self.websocket_task = asyncio.create_task(self.websocket_client.run())
            
            # 클라이언트 시작 로그 제거
            
        except Exception as e:
            self.logger.error(f"웹소켓 클라이언트 시작 실패: {e}", exc_info=True)
            

    async def init_kiwoom_client(self):
        """키움 REST API 클라이언트 초기화 (비동기)"""
        try:
            client = KiwoomRestClient('.env')
            if await client.connect():
                # 연결 직후 토큰 유효성 검사 및 갱신
                # [주의] is_token_expired 메서드가 KiwoomRestClient에 구현되어 있어야 함
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
                    self.logger.info("KIwoomRestClient 연결 직후 토큰 만료 감지 - 즉시 갱신 시도")
                    if await client.get_access_token():
                        self.logger.info("✅ 초기 토큰 갱신 성공")
                    else:
                        self.logger.error("❌ 초기 토큰 갱신 실패")
                        return None
                
                # REST API 클라이언트 초기화 로그 제거
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
            # 설정 저장 (동기 방식으로 안전하게 실행)
            try:
                self.save_settings_sync()
            except Exception as ex:
                self.logger.error(f"설정 저장 실패: {ex}", exc_info=True)
            
            # 키움 REST API 연결
            self.kiwoom_client = await self.init_kiwoom_client()
            
            if self.kiwoom_client and self.kiwoom_client.is_connected:
                # 연결 상태 업데이트
                self.parent.update_connection_ui(is_connected=True)
                
                # 거래 모드에 따른 메시지
                mode = "모의투자" if self.parent.trading_tab.tradingModeCombo.currentIndex() == 0 else "실제투자"
                logging.debug(f"키움 REST API 연결 성공! 거래 모드: {mode}")
                
                # 트레이더 객체 생성 (API 연결 성공 후 즉시)
                try:
                    if not hasattr(self.parent, 'trader') or not self.parent.trader:
                        buycount = int(self.parent.trading_tab.buycountEdit.text())
                        self.parent.trader = KiwoomTrader(self.kiwoom_client, buycount, self.parent)
                        
                        # 전략 객체 생성
                        self.parent.objstg = KiwoomStrategy(self.parent.trader, self.parent)
                        self.logger.debug("✅ 전략 객체(KiwoomStrategy) 생성 완료")

                        # ChartDataCache의 trader 속성 업데이트
                        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                            self.parent.chart_cache.trader = self.parent.trader
                            # ChartDataCache 업데이트 로그 제거
                    else:
                        # 트레이더 객체 존재 로그 제거
                        pass
                except Exception as trader_ex:
                    self.logger.error(f"트레이더 객체 생성 실패: {trader_ex}", exc_info=True)
                    
                
            else:
                self.logger.error("키움 REST API 연결 실패! .env 파일의 appkey와 appsecret을 확인해주세요.")
                self.parent.update_connection_ui(is_connected=False)
                
        except Exception as ex:
            self.logger.error(f"API 연결 처리 실패: {ex}", exc_info=True)
    
    async def _handle_connection_toggle_async(self):
        """연결/해제 버튼 클릭 비동기 처리"""
        try:
            # 키움 클라이언트가 있고 연결된 상태인지 확인
            is_connected = (hasattr(self, 'kiwoom_client') and 
                            self.kiwoom_client and 
                            self.kiwoom_client.is_connected)

            if is_connected:
                # --- 연결 해제 로직 ---
                self.logger.info("🔌 API 연결 해제를 시도합니다...")
                # 웹소켓 종료 (명시적 중지 요청)
                if hasattr(self, 'websocket_client') and self.websocket_client:
                    await self.websocket_client.stop()
                # REST 클라이언트 연결 해제
                await self.kiwoom_client.disconnect()
                
                # UI 업데이트 시그널 발생
                self.connection_status_changed.emit(False)
                self.logger.info("✅ API 연결이 해제되었습니다.")

            else:
                # --- 연결 로직 ---
                self.logger.info("🔌 API 연결을 시도합니다...")
                await self.handle_api_connection()
                await self.start_websocket_client()

                # 연결 성공 시 UI 업데이트는 post_login_setup에서 처리됨
                # 여기서는 연결 시도 상태를 UI에 반영할 수 있음 (예: 버튼 텍스트 변경)
                self.connection_status_changed.emit(True) # 임시로 연결됨 상태로 변경
        except Exception as ex:
            self.logger.error(f"연결/해제 처리 중 오류: {ex}", exc_info=True)

