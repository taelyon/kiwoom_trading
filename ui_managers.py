import logging
import asyncio
import configparser
import json
import os
import ast
import time
import io
import threading
import aiofiles
from datetime import datetime, timedelta
from PyQt6.QtCore import QObject, pyqtSignal, QTimer, Qt, QThread
from PyQt6.QtWidgets import QMessageBox, QListWidgetItem, QApplication
from utils import ApiLimitManager, safe_float_conversion, get_resource_path
from kiwoom_api import KiwoomRestClient, KiwoomWebSocketClient
from trader import KiwoomTrader
from backtester import KiwoomBacktester
from strategy import KiwoomStrategy

class LoginHandler(QObject):
    """로그인 및 연결 관리 클래스"""
    
    # 시그널 정의: 연결 상태가 변경될 때 UI 업데이트를 위해 사용
    connection_status_changed = pyqtSignal(bool)
    
    def __init__(self, parent_window):
        super().__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent_window
        self.config = configparser.RawConfigParser()
        self.kiwoom_client = None
    
    def get_target_buy_count(self):
        """settings.ini에서 최대투자 종목수 읽기"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            if config.has_option('BUYCOUNT', 'target_buy_count'):
                return config.getint('BUYCOUNT', 'target_buy_count')
            else:
                return 3  # 기본값
        except Exception as ex:
            self.logger.error(f"target_buy_count 읽기 실패: {ex}", exc_info=True)
            return 3  # 기본값
    
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
            self.config.read('settings.ini', encoding='utf-8')
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
            settings_path = get_resource_path('settings.ini')
            self.config.read(settings_path, encoding='utf-8')
            self.logger.debug("설정 저장을 위해 settings.ini 파일 다시 로드")
            
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
            settings_path = get_resource_path('settings.ini')
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
            client = KiwoomRestClient('settings.ini')
            if await client.connect():
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
                self.logger.error("키움 REST API 연결 실패! settings.ini 파일의 appkey와 appsecret을 확인해주세요.")
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
                # 웹소켓 종료
                if hasattr(self, 'websocket_client') and self.websocket_client:
                    await self.websocket_client.disconnect()
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


class DataManager:
    """데이터 조회 및 관리 매니저"""
    
    def __init__(self, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
        self.stock_code_map = {}  # 종목명: 종목코드 캐시
    
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
        """종목코드로 종목명 조회 - API 호출 제거됨"""
        # API 제한 초과 방지를 위해 종목코드만 반환
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

                # 코스피(0)와 코스닥(10) 종목 리스트를 모두 조회
                for market_code in ['0', '10']:
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

    async def _cache_all_stock_codes_async(self):
        """프로그램 시작 시 전체 종목 코드를 메모리에 캐싱"""
        self.stock_code_map.clear()
        try:
            if hasattr(self.parent, 'login_handler') and self.parent.login_handler.kiwoom_client:
                kiwoom_client = self.parent.login_handler.kiwoom_client
                for market_code in ['0', '10']: # KOSPI, KOSDAQ
                    headers = {'Content-Type': 'application/json;charset=UTF-8', 'authorization': f'Bearer {kiwoom_client.access_token}', 'api-id': 'ka10099'}
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
                                    self.stock_code_map[name] = code

            self.logger.info(f"✅ 전체 종목 코드 캐싱 완료: {len(self.stock_code_map)}개 종목")
        except Exception as ex:
            self.logger.error(f"전체 종목 코드 캐싱 실패: {ex}", exc_info=True)


class MonitoringManager:
    """모니터링 종목 관리 매니저"""
    
    def __init__(self, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
    
    async def add_stock_to_monitoring(self, code, name):
        """모니터링 리스트박스에 종목 추가"""
        try:
            # 중복 체크
            for i in range(self.parent.trading_tab.monitoringBox.count()):
                item_text = self.parent.trading_tab.monitoringBox.item(i).text()
                if code in item_text: # type: ignore
                    self.logger.debug(f"종목이 이미 모니터링 목록에 있습니다: {code}")
                    return True
            
            # 리스트박스에 추가
            item_text = f"{code}"
            self.parent.trading_tab.monitoringBox.addItem(item_text)
            self.logger.debug(f"✅ 모니터링 종목 추가: {item_text}")
            
            # 차트 캐시에 추가
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                await self.parent.chart_cache.add_monitoring_stock(code)

            # 모니터링 종목 수 변경에 따른 차트 업데이트 주기 조절
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                self.parent.chart_cache.update_chart_update_interval()
            
            # 실시간 체결 데이터 구독
            if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'websocket_client'):
                ws_client = self.parent.login_handler.websocket_client
                if ws_client and ws_client.connected:
                    # qasync 환경에서 안전하게 태스크 생성
                    try:
                        asyncio.get_running_loop()  # 루프 확인
                        asyncio.create_task(ws_client.subscribe_stock_execution_data([code], 'monitoring'))
                        self.logger.debug(f"📡 실시간 체결 데이터 구독: {code}")
                    except RuntimeError:
                        # 이벤트 루프가 없으면 무시 (정상적인 상황일 수 있음)
                        pass
            
            return True
            
        except Exception as ex:
            self.logger.error(f"모니터링 종목 추가 실패 ({code}): {ex}", exc_info=True)
            return False
    
    async def add_stock_to_monitoring_async(self, code):
        """모니터링 리스트박스에 종목 추가 (비동기 버전)"""
        try:
            # 중복 체크
            for i in range(self.parent.trading_tab.monitoringBox.count()):
                item_text = self.parent.trading_tab.monitoringBox.item(i).text()
                if code in item_text: # type: ignore
                    self.logger.debug(f"종목이 이미 모니터링 목록에 있습니다: {code}")
                    return True
            
            # 리스트박스에 추가
            item_text = f"{code}"
            self.parent.trading_tab.monitoringBox.addItem(item_text)
            self.logger.debug(f"✅ 모니터링 종목 추가: {item_text}")
            
            # 차트 캐시에 추가
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                await self.parent.chart_cache.add_monitoring_stock(code)

            # 모니터링 종목 수 변경에 따른 차트 업데이트 주기 조절
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                self.parent.chart_cache.update_chart_update_interval()
            
            # 실시간 체결 데이터 구독 (await 사용)
            if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'websocket_client'):
                ws_client = self.parent.login_handler.websocket_client
                if ws_client and ws_client.connected:
                    await ws_client.subscribe_stock_execution_data([code], 'monitoring')
            return True
        except Exception as ex:
            self.logger.error(f"모니터링 종목 추가 실패 (async) ({code}): {ex}")
            return False

    async def remove_stock_from_monitoring(self, code):
        """모니터링 리스트박스에서 종목 제거"""
        try:
            # 리스트박스에서 제거
            for i in range(self.parent.trading_tab.monitoringBox.count()):
                item = self.parent.trading_tab.monitoringBox.item(i)
                if item and code in item.text():
                    self.parent.trading_tab.monitoringBox.takeItem(i)
                    self.logger.debug(f"✅ 모니터링 종목 제거: {code}")
                    break
            
            # 차트 캐시에서도 제거 (모니터링 중단)
            # 차트 위젯이 제거된 종목을 표시하고 있었다면 차트 초기화
            if hasattr(self.parent, 'trading_tab') and hasattr(self.parent.trading_tab, 'realtime_chart_widget'):
                chart_widget = self.parent.trading_tab.realtime_chart_widget
                if chart_widget.current_code == code:
                    self.logger.debug(f"현재 차트 종목({code})이 모니터링에서 제거되어 차트를 초기화합니다.")
                    chart_widget.set_current_code(None)

            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                self.parent.chart_cache.remove_monitoring_stock(code)
            
            # 모니터링 종목 수 변경에 따른 차트 업데이트 주기 조절
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                self.parent.chart_cache.update_chart_update_interval()

            # 실시간 체결 데이터 구독 해제
            if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'websocket_client'):
                ws_client = self.parent.login_handler.websocket_client
                if ws_client and ws_client.connected:
                    # qasync 환경에서 안전하게 태스크 생성
                    try:
                        asyncio.get_running_loop()  # 루프 확인
                        asyncio.create_task(ws_client.unsubscribe_stock_execution_data([code]))
                        self.logger.debug(f"📡 실시간 구독 해제: {code}")
                    except RuntimeError:
                        self.logger.warning("⚠️ 이벤트 루프가 없어 구독 해제를 건너뜁니다")
            
            return True
            
        except Exception as ex:
            self.logger.error(f"모니터링 종목 제거 실패 ({code}): {ex}")
            return False
    
    async def remove_condition_stocks_from_monitoring(self, seq):
        """조건검색으로 추가된 종목들을 모니터링에서 제거"""
        try:
            # 조건검색 결과에서 종목 목록 가져오기
            if seq not in self.parent.condition_search_results:
                self.logger.debug(f"조건검색 결과 없음 (seq: {seq})")
                return
            
            stock_codes = self.parent.condition_search_results.get(seq, [])
            self.logger.info(f"조건검색 종목 제거 시작: {len(stock_codes)}개 (seq: {seq})")
            
            # 각 종목을 모니터링에서 제거
            for code in stock_codes:
                await self.remove_stock_from_monitoring(code)

            # 모니터링 종목 수 변경에 따른 차트 업데이트 주기 조절 (제거 후 한 번만 호출)
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                self.parent.chart_cache.update_chart_update_interval()
            
            # 조건검색 결과 딕셔너리에서 제거
            del self.parent.condition_search_results[seq]
            self.logger.info(f"조건검색 종목 제거 완료 (seq: {seq})")
            
        except Exception as ex:
            self.logger.error(f"조건검색 종목 제거 실패 (seq: {seq}): {ex}", exc_info=True)
    
    def extract_monitoring_stock_codes_enhanced(self):
        """모니터링 종목 코드 추출 및 로그 출력 - 강화된 예외 처리"""
        try:
            self.logger.debug("🔧 모니터링 종목 코드 추출 시작")
            self.logger.debug(f"현재 스레드: {threading.current_thread().name}")
            self.logger.debug(f"메인 스레드 여부: {threading.current_thread() is threading.main_thread()}")
            self.logger.debug("=" * 50)
            self.logger.debug("📋 모니터링 종목 코드 추출 시작")
            self.logger.debug("=" * 50)
            
            # 모니터링 종목 코드 추출
            monitoring_codes = self.get_monitoring_stock_codes() # type: ignore
            self.logger.debug(f"모니터링 종목 코드 추출: {monitoring_codes}")
            self.logger.debug(f"📋 모니터링 종목: {monitoring_codes}")
            
            self.logger.debug("=" * 50)
            self.logger.debug("✅ 모니터링 종목 코드 추출 완료")
            self.logger.debug("=" * 50)
            
            # 모니터링 종목 코드 추출 완료 후 차트 캐시 업데이트
            self.logger.debug(f"📋 모니터링 종목 코드 추출 완료: {monitoring_codes}")
            
            # 주식체결 실시간 구독 추가
            try:
                if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'kiwoom_client'):
                    # 웹소켓 클라이언트 참조가 제거되어 주식체결 구독 기능 비활성화
                    # 주식체결 구독은 별도로 관리되어야 함
                    self.logger.debug(f"주식체결 구독 기능은 별도로 관리됩니다: {monitoring_codes}")
                else:
                    self.logger.warning("⚠️ 키움 클라이언트가 초기화되지 않았습니다")
            except Exception as exec_sub_ex:
                self.logger.error(f"❌ 주식체결 구독 실패: {exec_sub_ex}", exc_info=True)
            
            # 차트 데이터 캐시 업데이트 (중요!)
            try:
                if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                    self.logger.debug(f"🔧 차트 캐시 업데이트 시작: {monitoring_codes}")
                    self.parent.chart_cache.update_monitoring_stocks(monitoring_codes)
                    self.logger.debug("✅ 차트 캐시 업데이트 완료")
                else:
                    self.logger.warning("⚠️ 차트 캐시가 초기화되지 않았습니다")
            except Exception as cache_ex:
                self.logger.error(f"❌ 차트 캐시 업데이트 실패: {cache_ex}", exc_info=True)
            
            return monitoring_codes
                
        except Exception as ex:
            self.logger.error(f"❌ 모니터링 종목 코드 추출 실패: {ex}", exc_info=True)
            return []
    
    def get_monitoring_stock_codes(self):
        """
        모니터링 박스에서 종목 코드 리스트 추출 (통합 버전)
        
        다양한 형식의 아이템 텍스트를 파싱하여 종목코드만 추출:
        - "종목코드 - 종목명" 형식
        - "종목코드 종목명" 형식 (공백 구분)
        - "종목코드" 단독
        
        Returns:
            list: 종목코드 리스트
        """
        try:
            stock_codes = []
            monitoring_box = self.parent.trading_tab.monitoringBox
            
            for i in range(monitoring_box.count()):
                item = monitoring_box.item(i)
                if not item:
                    continue
                    
                item_text = item.text().strip()
                if not item_text:
                    continue
                
                code = item_text
                
                # 'A' 접두사 제거
                if code.startswith('A'):
                    code = code[1:]
                
                # 6자리 종목코드만 허용
                if code and code.isdigit() and len(code) == 6:
                    stock_codes.append(code)
            
            self.logger.debug(f"모니터링 종목 코드 추출: {len(stock_codes)}개 - {stock_codes}")
            return stock_codes
            
        except Exception as ex:
            self.logger.error(f"모니터링 종목 코드 추출 실패: {ex}")
            return []
    
    def subscribe_realtime_execution_data(self, code):
        """실시간 체결 데이터 구독 시작"""
        try:
            self.logger = logging.getLogger(self.__class__.__name__)
            # 웹소켓 클라이언트가 연결되어 있는지 확인
            if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'websocket_client'):
                websocket_client = self.parent.login_handler.websocket_client
                if websocket_client and websocket_client.connected:
                    # qasync 환경에서 안전하게 태스크 생성
                    try:
                        asyncio.get_running_loop()  # 루프 확인
                        asyncio.create_task(websocket_client.subscribe_stock_execution_data([code], 'monitoring'))
                        self.logger.debug(f"📡 모니터링 종목 실시간 체결(0B) 구독 요청: {code}")
                    except RuntimeError:
                        self.logger.warning("⚠️ 이벤트 루프가 없어 구독을 건너뜁니다")
                else:
                    self.logger.warning(f"⚠️ 웹소켓이 연결되지 않아 실시간 구독을 시작할 수 없습니다: {code}")
            else:
                self.logger.warning(f"⚠️ 웹소켓 클라이언트가 없어 실시간 구독을 시작할 수 없습니다: {code}")
                
        except Exception as ex:
            self.logger.error(f"❌ 실시간 체결 데이터 구독 실패: {code} - {ex}")
    
    def unsubscribe_realtime_execution_data(self, code):
        """실시간 체결 데이터 구독 해제"""
        try:
            # 웹소켓 클라이언트가 연결되어 있는지 확인
            if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'websocket_client'):
                websocket_client = self.parent.login_handler.websocket_client
                if websocket_client and websocket_client.connected:
                    # qasync 환경에서 안전하게 태스크 생성
                    try:
                        asyncio.get_running_loop()  # 루프 확인
                        asyncio.create_task(websocket_client.unsubscribe_stock_execution_data([code]))
                        self.logger.debug(f"📡 실시간 체결 데이터 구독 해제: {code}")
                    except RuntimeError:
                        self.logger.warning("⚠️ 이벤트 루프가 없어 구독 해제를 건너뜁니다")
                else:
                    self.logger.warning(f"⚠️ 웹소켓이 연결되지 않아 실시간 구독 해제를 할 수 없습니다: {code}")
            else:
                self.logger.warning(f"⚠️ 웹소켓 클라이언트가 없어 실시간 구독 해제를 할 수 없습니다: {code}")
                
        except Exception as ex:
            self.logger.error(f"❌ 실시간 체결 데이터 구독 해제 실패: {code} - {ex}")


class StrategyManager:
    """전략 로드/저장 관리 매니저"""
    
    def __init__(self, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
    
    def load_strategy_combos(self):
        """전략 콤보박스에 settings.ini 값 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # 시그널을 잠시 끊어 중복 로드를 방지
            self.parent.trading_tab.comboStg.blockSignals(True)
            try:
                # 투자전략 콤보박스 로드
                self.parent.trading_tab.comboStg.clear()
                if config.has_section('STRATEGIES'):
                    for key, value in config.items('STRATEGIES'):
                        if key.startswith('stg_') or key == 'stg_integrated':
                            self.parent.trading_tab.comboStg.addItem(value)
                
                # 기본 전략 설정
                if config.has_option('SETTINGS', 'last_strategy'):
                    last_strategy = config.get('SETTINGS', 'last_strategy')
                    index = self.parent.trading_tab.comboStg.findText(last_strategy)
                    if index >= 0:
                        self.parent.trading_tab.comboStg.setCurrentIndex(index)
                        self.logger.debug(f"✅ 저장된 투자전략 복원: {last_strategy}")
                    else:
                        self.logger.warning(f"⚠️ 저장된 투자전략을 찾을 수 없습니다: {last_strategy}")
                else:
                    self.logger.debug("저장된 투자전략이 없습니다. 기본 전략을 사용합니다.")
            finally:
                # 시그널 다시 연결
                self.parent.trading_tab.comboStg.blockSignals(False)
                    
            # 매수전략 콤보박스 로드
            self.load_buy_strategies() # type: ignore
            
            # 매도전략 콤보박스 로드
            self.load_sell_strategies() # type: ignore
            
            # 초기 전략 내용 로드
            self.load_initial_strategy_content()
            
            self.logger.debug("투자전략 콤보박스 로드 완료")
            
        except Exception as ex:
            self.logger.error(f"전략 콤보박스 로드 실패: {ex}")
    
    def load_buy_strategies(self):
        """매수전략 콤보박스 로드"""
        self._load_strategy_list(self.parent.trading_tab.comboBuyStg, 'buy_stg_', 'buy')
    
    def load_sell_strategies(self):
        """매도전략 콤보박스 로드"""
        self._load_strategy_list(self.parent.trading_tab.comboSellStg, 'sell_stg_', 'sell')
    
    def _load_strategy_list(self, combo_widget, key_prefix, strategy_type):
        """전략 목록을 콤보박스에 로드"""
        try:
            combo_widget.clear()
            
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # 현재 선택된 투자전략 가져오기
            current_strategy = self.parent.trading_tab.comboStg.currentText()
            if not current_strategy:
                self.logger.warning("선택된 투자전략이 없습니다")
                return
            
            strategies = []

            if current_strategy == "통합 전략":
                # 급등주 + 갭상승 병합 로드 (숫자순)
                merge_sections = []
                if config.has_section('급등주'):
                    merge_sections.append('급등주')
                if config.has_section('갭상승'):
                    merge_sections.append('갭상승')

                for section in merge_sections:
                    # key_prefix로 필터링 후 숫자순 정렬
                    items = [(k, v) for k, v in config.items(section) if k.startswith(key_prefix)]
                    items.sort(key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 999)
                    for key, value in items:
                        try:
                            strategy_data = json.loads(value)
                            name = strategy_data.get('name', key)
                            # 구분을 위해 섹션 라벨 추가
                            display_name = f"[{section}] {name}"
                            strategies.append((f"{section}.{key}", display_name))
                        except json.JSONDecodeError:
                            self.logger.warning(f"전략 파싱 실패: {section}.{key}")
            else:
                # 해당 전략 섹션 확인
                if not config.has_section(current_strategy):
                    self.logger.warning(f"settings.ini에 [{current_strategy}] 섹션이 없습니다")
                    return
                
                # 전략 목록 추출
                for key in config[current_strategy]:
                    if key.startswith(key_prefix):
                        try:
                            strategy_data = json.loads(config[current_strategy][key])
                            strategies.append((key, strategy_data.get('name', key)))
                        except json.JSONDecodeError:
                            self.logger.warning(f"전략 파싱 실패: {key}")
            
            # 콤보박스에 추가
            for key, name in strategies:
                combo_widget.addItem(name, key)
            
            self.logger.debug(f"{strategy_type} 전략 {len(strategies)}개 로드 완료")
            
        except Exception as ex:
            self.logger.error(f"전략 목록 로드 실패 ({strategy_type}): {ex}")
    
    def save_current_strategy(self):
        """현재 선택된 투자전략을 settings.ini에 저장"""
        try:
            current_strategy = self.parent.trading_tab.comboStg.currentText()
            if not current_strategy:
                self.logger.debug("저장할 투자전략이 없습니다")
                return
            
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # [Strategy] 섹션 대신 [SETTINGS].last_strategy에 통합 저장
            if not config.has_section('SETTINGS'):
                config.add_section('SETTINGS')
            config.set('SETTINGS', 'last_strategy', current_strategy)
            
            with open('settings.ini', 'w', encoding='utf-8') as f:
                config.write(f)
            
            self.logger.debug(f"✅ 현재 투자전략 저장(SETTINGS.last_strategy): {current_strategy}")
            
        except Exception as ex:
            self.logger.error(f"투자전략 저장 실패: {ex}")
    
    def load_initial_strategy_content(self):
        """초기 전략 내용을 텍스트박스에 로드"""
        try:
            # 매수전략 초기 내용 로드 # type: ignore
            if self.parent.trading_tab.comboBuyStg.count() > 0:
                current_buy_strategy = self.parent.trading_tab.comboBuyStg.currentText()
                self.load_strategy_content(current_buy_strategy, 'buy')
            
            # 매도전략 초기 내용 로드
            if self.parent.trading_tab.comboSellStg.count() > 0:
                current_sell_strategy = self.parent.trading_tab.comboSellStg.currentText()
                self.load_strategy_content(current_sell_strategy, 'sell')
                
        except Exception as ex:
            self.logger.error(f"초기 전략 내용 로드 실패: {ex}")
    
    def load_strategy_content(self, strategy_name, strategy_type):
        """전략 내용을 텍스트 위젯에 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            current_strategy = self.parent.trading_tab.comboStg.currentText()

            target_section = current_strategy
            target_key = None
            display_name = strategy_name

            # 통합 전략: 콤보 표시명은 "[섹션] 이름" → 섹션/이름 분리
            if current_strategy == "통합 전략" and strategy_name.startswith('['):
                try:
                    end_idx = strategy_name.find(']')
                    section_label = strategy_name[1:end_idx]
                    display_name = strategy_name[end_idx+2:]
                    if config.has_section(section_label):
                        target_section = section_label
                except Exception:
                    pass

            if not config.has_section(target_section):
                return

            # 전략 키 찾기 (섹션 내)
            for key, value in config.items(target_section):
                try:
                    strategy_data = eval(value)
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
                strategy_data = eval(config.get(target_section, target_key))
                content = strategy_data.get('content', '')
                
                # 텍스트 위젯에 표시
                if strategy_type == 'buy':
                    self.parent.trading_tab.buystgInputWidget.setPlainText(content)
                elif strategy_type == 'sell':
                    self.parent.trading_tab.sellstgInputWidget.setPlainText(content)
                    
        except Exception as ex:
            self.logger.error(f"전략 내용 로드 실패: {ex}")
    
    async def _clear_monitoring_list(self):
        """모니터링 리스트와 관련된 상태를 초기화합니다. (단, 보유 종목은 제외)"""
        self.logger.info("🔄 투자 전략 변경으로 인해 기존 모니터링 목록을 초기화합니다.")
        
        # 1. 현재 보유 중인 종목 코드를 가져옵니다.
        holding_codes = set()
        if hasattr(self.parent, 'trading_tab') and hasattr(self.parent.trading_tab, 'boughtBox'):
            for i in range(self.parent.trading_tab.boughtBox.count()):
                item = self.parent.trading_tab.boughtBox.item(i)
                if item:
                    holding_codes.add(item.text().split()[0])
        self.logger.debug(f"보유 종목은 모니터링에서 제외하지 않습니다: {list(holding_codes)}")

        # 2. 서버에 등록된 *모든* 조건검색을 해제 시도 (상태 불일치 해결)
        if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
            all_seqs = [c['seq'] for c in self.parent.condition_search_list]
            self.logger.debug(f"🔍 모든 조건검색 해제 시도: {all_seqs}")
            for seq in all_seqs:
                await self.parent.stop_condition_realtime(seq)
        
        # 3. 클라이언트의 활성 목록도 비웁니다.
        if hasattr(self.parent, 'active_realtime_conditions'):
            self.parent.active_realtime_conditions.clear()
        self.logger.debug("✅ 모든 실시간 조건검색 모니터링 중단 완료.")

        # 4. 모니터링 리스트 박스에서 보유 종목을 제외하고 비우기
        monitoring_box = self.parent.trading_tab.monitoringBox
        for i in range(monitoring_box.count() - 1, -1, -1):
            item = monitoring_box.item(i)
            if item and item.text().split()[0] not in holding_codes:
                monitoring_box.takeItem(i)
        
        # 5. 모니터링 종목이 비워졌으므로 차트 업데이트 주기 조절 (타이머 중지)
        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
            self.parent.chart_cache.update_chart_update_interval()

        # 6. 차트 캐시에서 보유 종목이 아닌 것들만 제거
        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
            codes_to_remove = [code for code in self.parent.chart_cache.cache.keys() if code not in holding_codes]
            for code in codes_to_remove:
                self.parent.chart_cache.remove_monitoring_stock(code)
            self.logger.debug(f"차트 캐시에서 {len(codes_to_remove)}개 종목 제거 완료.")

    async def stg_changed(self):
        """전략 변경 이벤트 핸들러 (비동기)"""
        try:
            # 수동 변경 플래그 설정
            self.parent.condition_search_manager.is_manual_change = True

            strategy_name = self.parent.trading_tab.comboStg.currentText()
            self.logger.debug(f"투자 전략 변경: {strategy_name}")

            # 초기 로딩 중에는 저장하지 않음
            if self.parent.is_loading_strategy:
                self.logger.debug("초기화 중... 전략 저장을 건너뜁니다.")
                return
            
            # 기존 모니터링 초기화 (비동기 호출)
            await self._clear_monitoring_list()
            await asyncio.sleep(1.0)  # 0.5초 대기

            # 현재 선택된 전략을 settings.ini에 저장
            self.save_current_strategy()
            
            # 투자전략 변경 시 매수/매도 전략 목록을 항상 새로고침합니다.
            self.load_buy_strategies()
            self.load_sell_strategies()
            
            # 변경된 전략의 첫 번째 매수/매도 전략 내용 자동 로드
            self.load_initial_strategy_content()

            # 조건검색식인지 확인 (조건검색 목록에 있는지 확인)
            if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                condition_names = [condition['title'] for condition in self.parent.condition_search_list]
                if strategy_name in condition_names:
                    # 조건검색식 선택 시 바로 실행 (비동기)
                    try:
                        asyncio.create_task(self.parent.condition_search_manager.handle_condition_search())
                    except RuntimeError:
                        self.logger.warning("⚠️ 이벤트 루프가 없어 조건검색을 실행할 수 없습니다")
            
            # 통합 전략인 경우 모든 조건검색식 실행
            if strategy_name == "통합 전략":
                if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                    self.logger.debug("🔍 통합 전략 실행: 모든 조건검색식 적용 (ConditionSearchManager)")
                    try:
                        asyncio.create_task(self.parent.condition_search_manager.handle_integrated_condition_search())
                    except RuntimeError:
                        logging.warning("⚠️ 이벤트 루프가 없어 통합 전략을 실행할 수 없습니다")
            
        except Exception as ex:
            self.logger.error(f"전략 변경 실패: {ex}")
    
    def buy_stg_changed(self):
        """매수 전략 변경 이벤트 핸들러"""
        try:
            strategy_name = self.parent.trading_tab.comboBuyStg.currentText()
            self.logger.debug(f"매수 전략 변경: {strategy_name}")
            
            # 매수 전략 내용을 텍스트 위젯에 표시
            self.load_strategy_content(strategy_name, 'buy')
            
        except Exception as ex:
            self.logger.error(f"매수 전략 변경 실패: {ex}")
    
    def sell_stg_changed(self):
        """매도 전략 변경 이벤트 핸들러"""
        try:
            strategy_name = self.parent.trading_tab.comboSellStg.currentText()
            self.logger.debug(f"매도 전략 변경: {strategy_name}")
            
            # 매도 전략 내용을 텍스트 위젯에 표시
            self.load_strategy_content(strategy_name, 'sell')
            
        except Exception as ex:
            self.logger.error(f"매도 전략 변경 실패: {ex}")
    
    def _save_strategy(self, text_widget, combo_widget, key_prefix, strategy_type):
        """전략 저장 (공통 로직)
        
        Args:
            text_widget: 전략 내용이 있는 텍스트 위젯
            combo_widget: 전략 선택 콤보박스 위젯
            key_prefix: 전략 키 접두사 ('buy_stg_' 또는 'sell_stg_')
            strategy_type: 전략 타입 ('매수' 또는 '매도')
        """
        try:
            strategy_text = text_widget.toPlainText()
            current_strategy = self.parent.trading_tab.comboStg.currentText()
            current_strategy_name = combo_widget.currentText()
            key_from_combobox = combo_widget.currentData()

            # settings.ini 파일 업데이트
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')

            target_section = current_strategy
            target_key = key_from_combobox

            # "통합 전략"일 경우, 실제 섹션과 키를 찾아서 업데이트
            if current_strategy == "통합 전략":
                if '.' in key_from_combobox:
                    section, key = key_from_combobox.split('.', 1)
                    target_section = section
                    target_key = key
                else:
                    self.logger.error(f"통합 전략 저장 오류: 잘못된 키 형식 - {key_from_combobox}")
                    return

            if not config.has_section(target_section):
                self.logger.error(f"{strategy_type} 전략 저장 실패: 섹션을 찾을 수 없음 - [{target_section}]")
                return

            if not config.has_option(target_section, target_key):
                logging.error(f"{strategy_type} 전략 저장 실패: 키를 찾을 수 없음 - {target_key}")
                return

            # 기존 전략 데이터 로드 및 수정
            try:
                strategy_json_str = config.get(target_section, target_key)
                strategy_data = ast.literal_eval(strategy_json_str) # json.loads 대신 ast.literal_eval 사용
                strategy_data['content'] = strategy_text
                # json.dumps를 사용하여 문자열로 변환
                config.set(target_section, target_key, json.dumps(strategy_data, ensure_ascii=False))
            except (ValueError, SyntaxError, KeyError) as e:
                self.logger.error(f"전략 데이터 파싱 또는 수정 실패: {e}")
                return

            # 파일 저장
            with open('settings.ini', 'w', encoding='utf-8') as configfile:
                config.write(configfile)

            self.logger.debug(f"{strategy_type} 전략 '{current_strategy_name}'이 저장되었습니다.")

            # 전략 즉시 반영: KiwoomStrategy 객체의 설정을 다시 로드
            if hasattr(self.parent, 'objstg') and self.parent.objstg:
                self.parent.objstg.load_strategy_config()
                self.logger.info(f"✅ {strategy_type} 전략이 즉시 반영되었습니다.")

        except Exception as ex:
            self.logger.error(f"{strategy_type} 전략 저장 실패: {ex}")
    
    def _save_strategy_legacy(self, text_widget, combo_widget, key_prefix, strategy_type):
        """전략 저장 (레거시 - eval 사용)"""
        try:
            strategy_text = text_widget.toPlainText()
            current_strategy_name = combo_widget.currentText()
            key_from_combobox = combo_widget.currentData()

            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')

            strategy_data = eval(config.get(current_strategy_name, key_from_combobox))
            strategy_data['content'] = strategy_text
            config.set(current_strategy_name, key_from_combobox, str(strategy_data))

            with open('settings.ini', 'w', encoding='utf-8') as configfile:
                config.write(configfile)
        except Exception as ex:
            self.logger.error(f"{strategy_type} 전략 저장 실패 (레거시): {ex}")

    def save_buystrategy(self):
        """매수 전략 저장"""
        self._save_strategy(self.parent.trading_tab.buystgInputWidget, self.parent.trading_tab.comboBuyStg, 'buy_stg_', '매수')
    
    def save_sellstrategy(self):
        """매도 전략 저장"""
        self._save_strategy(self.parent.trading_tab.sellstgInputWidget, self.parent.trading_tab.comboSellStg, 'sell_stg_', '매도')


class TradingManager(QObject):
    """매매 실행 관리 매니저"""
    
    # 메시지 박스 표시를 위한 시그널 정의
    show_message_signal = pyqtSignal(str, str)
    
    def __init__(self, parent):
        super().__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
        self.show_message_signal.connect(parent.show_message_box)

    def get_target_buy_count(self):
        """settings.ini에서 최대투자 종목수 읽기"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            if config.has_option('BUYCOUNT', 'target_buy_count'):
                return config.getint('BUYCOUNT', 'target_buy_count')
            else:
                return 3  # 기본값 # type: ignore
        except Exception as ex:
            self.logger.error(f"target_buy_count 읽기 실패: {ex}")
            return 3  # 기본값

    def buycount_setting(self):
        """투자 종목수 설정"""
        try:
            buycount = int(self.parent.trading_tab.buycountEdit.text())
            if buycount > 0:
                # settings.ini 파일에 저장
                config = configparser.RawConfigParser()
                config.read('settings.ini', encoding='utf-8')
                
                # BUYCOUNT 섹션이 없으면 생성
                if not config.has_section('BUYCOUNT'):
                    config.add_section('BUYCOUNT')
                
                # 값 설정
                config.set('BUYCOUNT', 'target_buy_count', str(buycount))
                
                # 파일에 저장
                with open('settings.ini', 'w', encoding='utf-8') as configfile:
                    config.write(configfile)
                
                # 메모리에도 저장 (하위 호환성)
                if hasattr(self.parent, 'trader'):
                    self.parent.trader.buycount = buycount
                
                self.logger.info(f"✅ 최대 투자 종목수 설정 완료: {buycount}종목")
                # QMessageBox.information(self.parent, "설정 완료", f"최대 투자 종목수가 {buycount}종목으로 설정되었습니다.")
            else:
                self.logger.warning("1 이상의 숫자를 입력해주세요.")
                # QMessageBox.warning(self.parent, "입력 오류", "1 이상의 숫자를 입력해주세요.")
        except ValueError:
            self.logger.warning("올바른 숫자를 입력해주세요.")
            # QMessageBox.warning(self.parent, "입력 오류", "올바른 숫자를 입력해주세요.")
        except Exception as ex:
            self.logger.error(f"투자 종목수 설정 실패: {ex}")
            # QMessageBox.critical(self.parent, "설정 실패", f"설정 중 오류가 발생했습니다:\n{ex}")
    
    async def delete_select_item(self):
        """선택된 종목 삭제"""
        try:
            current_item = self.parent.trading_tab.monitoringBox.currentItem()
            if not current_item:
                self.logger.warning("삭제할 종목을 선택해주세요.")
                return

            item_text = current_item.text()
            code = item_text.split()[0]

            # MonitoringManager를 통해 종목 제거 (UI, 캐시, 웹소켓 구독 해제 모두 처리)
            if hasattr(self.parent, 'monitoring_manager'):
                await self.parent.monitoring_manager.remove_stock_from_monitoring(code)

        except Exception as ex:
            self.logger.error(f"종목 삭제 실패: {ex}")
    
    async def add_stock_to_list(self):
        """투자 대상 종목 리스트에 종목 추가 (API 큐를 통한 차트 데이터 수집 후 추가)"""
        try:
            stock_input = self.parent.trading_tab.stockInputEdit.text().strip()
            if not stock_input:
                self.logger.warning("종목명 또는 종목코드를 입력해주세요.")
                return
            
            # 종목코드 정규화 (6자리 숫자로 변환)
            stock_code, stock_name = await self.parent.data_manager.normalize_stock_input(stock_input)
            
            # 종목명 검색 실패 시 처리
            if stock_code is None or stock_name is None:
                self.logger.error(f"❌ 종목을 찾을 수 없습니다: {stock_input}")
                return
            
            # 이미 모니터링에 존재하는지 확인
            for i in range(self.parent.trading_tab.monitoringBox.count()):
                existing_code = self.parent.trading_tab.monitoringBox.item(i).text()
                if existing_code == stock_code:
                    self.logger.warning(f"'{stock_name}' 종목이 이미 모니터링에 존재합니다.")
                    return
            
            # 입력 필드 초기화
            self.parent.trading_tab.stockInputEdit.clear()
            
            # API 큐에 추가 (차트 데이터 수집 후 모니터링에 추가)
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                if self.parent.chart_cache.add_stock_to_api_queue(stock_code):
                    self.logger.debug(f"📋 수동 추가 종목을 API 큐에 추가: {stock_code}")
                    self.logger.debug("📋 차트 데이터 수집 완료 후 모니터링에 추가됩니다")
                else:
                    self.logger.warning(f"⚠️ API 큐 추가 실패: {stock_code}")
            else:
                self.logger.error("❌ chart_cache가 없어 종목을 추가할 수 없습니다")
            
        except Exception as ex:
            self.logger.error(f"종목 추가 실패: {ex}")
    
    def trading_mode_changed(self):
        """거래 모드 변경 이벤트 핸들러"""
        try:
            mode = "모의투자" if self.parent.trading_tab.tradingModeCombo.currentIndex() == 0 else "실제투자"
            self.logger.debug(f"거래 모드 변경: {mode}")
            
            # 키움 클라이언트의 is_mock 설정 업데이트
            if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'kiwoom_client') and self.parent.login_handler.kiwoom_client:
                is_mock = (self.parent.trading_tab.tradingModeCombo.currentIndex() == 0)
                self.parent.login_handler.kiwoom_client.is_mock = is_mock
                self.logger.debug(f"키움 클라이언트 모의투자 설정 업데이트: {is_mock}")
            
            # 연결된 상태라면 재연결 안내 (로그로만 표시)
            if hasattr(self.parent, 'trader') and self.parent.trader and self.parent.trader.client and self.parent.trader.client.is_connected:
                self.logger.debug(f"거래 모드가 {mode}로 변경되었습니다. 새로운 설정을 적용하려면 API를 재연결해주세요.")
                
        except Exception as ex:
            self.logger.error(f"거래 모드 변경 실패: {ex}")
    
    async def sell_all_item(self, is_auto=False):
        """전체 매도 (키움 REST API 기반) (비동기)
        
        Args:
            is_auto: True면 자동 청산, False면 수동 전체 매도
        """
        # 다른 비동기 작업과의 충돌을 막기 위해 락을 사용합니다.
        if self.parent.trading_lock.locked():
            self.logger.warning("⚠️ 다른 수동 매매 작업이 이미 진행 중입니다.")
            return
        # 자동매매 타이머 일시 중지
        autotrader = getattr(self.parent, 'autotrader', None)
        chart_cache = getattr(self.parent, 'chart_cache', None)
        if autotrader:
            autotrader.stop_auto_trading()
        if chart_cache and chart_cache.update_timer:
            chart_cache.update_timer.stop()
        
        # 로그 메시지 구분
        if is_auto:
            self.logger.info("자동 청산 - 모든 주기적 타이머 일시 중지")
        else:
            self.logger.info("수동 전체 매도 시작 - 모든 주기적 타이머 일시 중지")
        try:
            async with self.parent.trading_lock:
                if self.parent.trading_tab.boughtBox.count() == 0:
                    self.logger.warning("매도할 종목이 없습니다.")
                    # 타이머 재시작
                    self._restart_timers_after_manual_trade(autotrader, chart_cache)
                    return

                # 로그 메시지 구분
                if is_auto:
                    self.logger.info("🔄 자동 청산 매도 시작")
                else:
                    self.logger.info("🔄 전체 매도 시작")
                
                # 보유 종목 목록 생성
                sell_items = []
                for i in range(self.parent.trading_tab.boughtBox.count()):
                    item = self.parent.trading_tab.boughtBox.item(i)
                    item_text = item.text()
                    code = item_text.split()[0]
                    sell_items.append(code)
                
                # 각 종목에 대해 매도 주문 실행
                success_count = 0
                for code in sell_items:
                    try:
                        # 보유 수량 조회 (웹소켓/REST API 이중 체크)
                        quantity = 0
                        
                        # 1차: 웹소켓 실시간 잔고 데이터에서 보유 수량 조회 시도
                        if (hasattr(self.parent, 'login_handler') and self.parent.login_handler and 
                            hasattr(self.parent.login_handler, 'websocket_client') and self.parent.login_handler.websocket_client and
                            hasattr(self.parent.login_handler.websocket_client, 'balance_data')):
                            
                            ws_client = self.parent.login_handler.websocket_client
                            balance_data = ws_client.balance_data
                            
                            if code in balance_data:
                                quantity = balance_data[code].get('quantity', 0)
                                self.logger.debug(f"💰 웹소켓 잔고: {code} {quantity}주")
                        
                        # 2차: 웹소켓 데이터가 없거나 수량이 0이면 REST API로 조회
                        if quantity <= 0:
                            try:
                                if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                                    balance_result = await self.parent.login_handler.kiwoom_client.get_acnt_balance()
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
                        
                        # 수량 확인
                        if quantity <= 0:
                            self.logger.warning(f"⚠️ {code} 보유 수량 없음 - 건너뜀")
                            continue
                        
                        # 매도 주문 실행 (재시도 로직 포함)
                        if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                            max_retries = 3
                            retry_delay = 1.0  # 초기 대기 시간
                            success = False
                            
                            for attempt in range(max_retries):
                                try:
                                    success = await self.parent.login_handler.kiwoom_client.place_sell_order(code, quantity, 0, "market")
                                    
                                    if success:
                                        success_count += 1
                                        if is_auto:
                                            self.logger.info(f"✅ 자동 청산 성공: {code} {quantity}주")
                                        else:
                                            self.logger.info(f"✅ 전체 매도 성공: {code} {quantity}주")
                                        break  # 성공하면 재시도 중단
                                    else:
                                        self.logger.warning(f"⚠️ 매도 주문 실패 (시도 {attempt + 1}/{max_retries}): {code}")
                                        
                                except Exception as order_ex:
                                    error_msg = str(order_ex)
                                    
                                    # HTTP 429 (Rate Limit) 오류 감지
                                    if "429" in error_msg or "Too Many Requests" in error_msg:
                                        if attempt < max_retries - 1:
                                            self.logger.warning(f"⚠️ API Rate Limit 초과 - {retry_delay}초 후 재시도 ({attempt + 1}/{max_retries}): {code}")
                                            await asyncio.sleep(retry_delay)
                                            retry_delay *= 2  # 지수 백오프
                                            continue
                                        else:
                                            self.logger.error(f"❌ API Rate Limit 초과 - 최대 재시도 횟수 도달: {code}")
                                    else:
                                        self.logger.error(f"❌ 매도 주문 오류: {code} - {error_msg}")
                                        break
                            
                            if not success:
                                if is_auto:
                                    self.logger.error(f"❌ 자동 청산 실패: {code}")
                                else:
                                    self.logger.error(f"❌ 전체 매도 실패: {code}")
                            
                            # API 요청 제한을 피하기 위해 종목 간 지연 추가
                            if len(sell_items) > 1:
                                await asyncio.sleep(0.8)  # 0.5초 -> 0.8초로 증가
                                
                    except Exception as item_ex:
                        self.logger.error(f"❌ {code} 매도 중 오류: {item_ex}")
                
                # 결과 로그
                if success_count > 0:
                    if is_auto:
                        self.logger.info(f"✅ 자동 청산 완료: {success_count}개 종목 매도")
                    else:
                        self.logger.info(f"✅ 전체 매도 완료: {success_count}개 종목")
                else:
                    if is_auto:
                        self.logger.error("❌ 자동 청산 실패")
                    else:
                        self.logger.error("❌ 전체 매도 실패")
        except Exception as ex:
            self.logger.error(f"전체 매도 작업 중 오류 발생: {ex}", exc_info=True)
            # QMessageBox.critical(self.parent, "전체 매도 오류", f"전체 매도 중 오류가 발생했습니다: {ex}")
        finally:
            # 자동매매 타이머 다시 시작
            await self._restart_timers_after_manual_trade(autotrader, chart_cache)
    
    async def sell_item(self):
        """종목 매도 - 보유수량 전량 매도 (키움 REST API 기반) (비동기)"""
        if self.parent.trading_lock.locked():
            self.logger.warning("⚠️ 다른 수동 매매 작업이 이미 진행 중입니다.")
            return
        # 자동매매 타이머 일시 중지
        autotrader = getattr(self.parent, 'autotrader', None)
        chart_cache = getattr(self.parent, 'chart_cache', None)
        if autotrader:
            autotrader.stop_auto_trading()
        if chart_cache and chart_cache.update_timer:
            chart_cache.update_timer.stop()
        self.logger.debug("수동 매도 시작 - 모든 주기적 타이머 일시 중지")
        try:
            async with self.parent.trading_lock:
                current_item = self.parent.trading_tab.boughtBox.currentItem()
                if not current_item:
                    self.logger.warning("매도할 종목을 선택해주세요.")
                    # QMessageBox.warning(self.parent, "선택 오류", "매도할 종목을 선택해주세요.")
                    # 타이머 재시작
                    await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                    return

                item_text = current_item.text()
                code = item_text.split()[0]
                
                self.logger.debug(f"매도 요청: {code}")
                
                quantity = 0
                
                # 1차: REST API로 주문가능수량 조회
                try:
                    if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                        balance_result = await self.parent.login_handler.kiwoom_client.get_acnt_balance()
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

                # 2차: REST API 조회 실패 또는 수량 0일 때 웹소켓 데이터로 재확인
                if quantity <= 0:
                    if (hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'websocket_client')):
                        ws_balance_data = self.parent.login_handler.websocket_client.balance_data
                        if ws_balance_data and code in ws_balance_data:
                            quantity = ws_balance_data[code].get('order_available_qty', 0)
                            self.logger.info(f"💰 웹소켓 잔고 조회 (Fallback): {code} 주문가능수량 {quantity}주")
                
                if quantity <= 0:
                    self.logger.warning(f"⚠️ 보유 수량 없음: {code}")
                    # QMessageBox.warning(self.parent, "매도 불가", f"{code} 보유 수량이 없습니다.\n웹소켓과 REST API 모두 확인했습니다.")
                    await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                    return                
                
                if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                    success = await self.parent.login_handler.kiwoom_client.place_sell_order(code, quantity, 0, "market")
                    if success:
                        self.logger.info(f"✅ 매도 주문 성공: {code} {quantity}주 전량 매도")
                    else:
                        self.logger.error(f"❌ 매도 주문 실패: {code}")
                        # QMessageBox.warning(self.parent, "매도 실패", f"{code} 매도 주문이 실패했습니다.")
                else:
                    self.logger.error("키움 클라이언트가 초기화되지 않았습니다")
                    # QMessageBox.warning(self.parent, "오류", "키움 클라이언트가 초기화되지 않았습니다.")
        except Exception as ex:
            self.logger.error(f"매도 작업 중 오류 발생: {ex}", exc_info=True)
            # QMessageBox.critical(self.parent, "매도 오류", f"매도 중 오류가 발생했습니다: {ex}")
        finally:
            # 자동매매 타이머 다시 시작
            await self._restart_timers_after_manual_trade(autotrader, chart_cache)
    
    async def buy_item(self):
        """종목 매입 - 자동 매입가능수량 계산 (키움 REST API 기반) (비동기)"""
        if self.parent.trading_lock.locked():
            self.logger.warning("⚠️ 다른 수동 매매 작업이 이미 진행 중입니다.")
            return
        # 자동매매 타이머 일시 중지
        autotrader = getattr(self.parent, 'autotrader', None)
        chart_cache = getattr(self.parent, 'chart_cache', None)
        if autotrader:
            autotrader.stop_auto_trading()
        if chart_cache and chart_cache.update_timer:
            chart_cache.update_timer.stop()
        self.logger.debug("수동 매수 시작 - 모든 주기적 타이머 일시 중지")
        try:
            async with self.parent.trading_lock:
                current_item = self.parent.trading_tab.monitoringBox.currentItem()
                if not current_item:
                    self.logger.warning("매입할 종목을 선택해주세요.")
                    # self.show_message_signal.emit("선택 오류", "매입할 종목을 선택해주세요.") # QMessageBox 직접 호출 대신 시그널 사용
                    await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                    return

                # "종목코드 - 종목명" 또는 "종목코드" 형식에서 종목코드만 정확히 추출
                item_text = current_item.text()
                code = item_text.split()[0]
                
                if hasattr(self.parent, 'boughtBox'):
                    for i in range(self.parent.boughtBox.count()):
                        bought_item_text = self.parent.trading_tab.boughtBox.item(i).text()
                        # "종목코드 - 종목명" 또는 "종목코드" 형식에서 종목코드만 추출하여 비교
                        bought_code = bought_item_text.split()[0]
                        
                        if bought_code == code:
                            self.logger.info(f"⚠️ 매수 주문 취소: {code}는 이미 보유 중인 종목입니다.")
                            # self.show_message_signal.emit("매수 불가", f"{code}는 이미 보유 중인 종목입니다.") # QMessageBox 직접 호출 대신 시그널 사용
                            await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                            return
                
                quantity = 0
                try:
                    if not hasattr(self.parent, 'trader') or not self.parent.trader:
                        self.logger.error("⚠️ trader가 초기화되지 않았습니다 (API 연결이 필요합니다)")
                        # self.show_message_signal.emit("오류", "API에 먼저 연결해주세요.") # QMessageBox 직접 호출 대신 시그널 사용
                        await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                        return
                
                    available_cash = await self.parent.trader.get_available_cash()
                    if available_cash <= 0:
                        self.logger.warning(f"⚠️ 매수 주문 취소: 투자가능금액 부족 ({available_cash:,.0f}원)")
                        # self.show_message_signal.emit("매수 불가", f"투자가능금액이 부족합니다.\n현재: {available_cash:,.0f}원") # QMessageBox 직접 호출 대신 시그널 사용
                        await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                        return
                    
                    available_buy_count = self.parent.login_handler.get_available_buy_count()
                    if available_buy_count <= 0:
                        self.logger.warning(f"⚠️ 매수 주문 취소: 최대 보유 종목 수 도달")
                        # self.show_message_signal.emit("매수 불가", "최대 보유 종목 수에 도달했습니다.") # QMessageBox 직접 호출 대신 시그널 사용
                        await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                        return
                    
                    current_price = 0
                    price_source = ""
                    
                    # 1순위: ChartDataCache에서 현재가 조회 (올바른 경로로 수정)
                    if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                        cached_data = self.parent.chart_cache.get_cached_data(code)
                        if cached_data and cached_data.get('tic_data'):
                            tic_data = cached_data['tic_data']
                            if tic_data.get('close') and len(tic_data['close']) > 0:
                                current_price = float(tic_data['close'][-1])
                                price_source = "캐시"
                    
                    # 2순위: 캐시 조회 실패 시 REST API로 현재가 조회
                    if current_price <= 0:
                        try:
                            current_price = await self.parent.trader.get_current_price(code)
                            if current_price > 0: price_source = "API"
                        except Exception as price_ex:
                            self.logger.debug(f"현재가 조회 실패: {price_ex}")

                    if current_price <= 0:
                        self.logger.error(f"❌ 현재가 조회에 실패하여 매수 주문을 취소합니다: {code}")
                        # self.show_message_signal.emit("매수 불가", f"{code}의 현재가 조회에 실패했습니다.")
                        await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                        return
                    
                    budget_per_stock = available_cash // available_buy_count
                    quantity = int(budget_per_stock / current_price)
                    if quantity <= 0: quantity = 1
                    
                    self.logger.debug(f"🛒 {code} 매수: {quantity}주 @ 시장가 (예산 {budget_per_stock:,.0f}원, 현재가 {current_price:,.0f}원/{price_source})")
                    
                except Exception as calc_ex:
                    self.logger.error(f"❌ 매수 수량 계산 실패: {calc_ex}")
                    # self.show_message_signal.emit("오류", f"매수 수량 계산 중 오류가 발생했습니다:\n{calc_ex}") # QMessageBox 직접 호출 대신 시그널 사용
                    await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                    return
                
                if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                    success = await self.parent.login_handler.kiwoom_client.place_buy_order(code, quantity, 0, "market")
                    if not success:
                        self.logger.error(f"❌ 매수 주문 실패: {code}")
                        # self.show_message_signal.emit("매수 실패", f"{code} 매수 주문이 실패했습니다.") # QMessageBox 직접 호출 대신 시그널 사용
                else:
                    self.logger.error("키움 클라이언트가 초기화되지 않았습니다")
                    # self.show_message_signal.emit("오류", "키움 클라이언트가 초기화되지 않았습니다.") # QMessageBox 직접 호출 대신 시그널 사용
        except Exception as ex:
            self.logger.error(f"매입 작업 중 오류 발생: {ex}", exc_info=True)
            # self.show_message_signal.emit("매입 오류", f"매입 중 오류가 발생했습니다:\n{ex}") # QMessageBox 직접 호출 대신 시그널 사용
        finally:
            # 자동매매 타이머 다시 시작
            await self._restart_timers_after_manual_trade(autotrader, chart_cache)
    
    async def _restart_timers_after_manual_trade(self, autotrader, chart_cache):
        """수동 매매 후 타이머들을 재시작하는 헬퍼 함수"""
        await asyncio.sleep(1)  # 1초 비동기 대기
        if autotrader:
            autotrader.start_auto_trading()
        if chart_cache:
            chart_cache.start()
        self.logger.debug("수동 매매 완료 - 모든 주기적 타이머 다시 시작")


class BacktestWorker(QThread):
    """백테스팅을 비동기로 실행하기 위한 워커 스레드"""
    finished_signal = pyqtSignal(object, bool)  # backtester, success
    error_signal = pyqtSignal(str)

    def __init__(self, backtester, codes, start_date, end_date, strategy_name):
        super().__init__()
        self.backtester = backtester
        self.codes = codes
        self.start_date = start_date
        self.end_date = end_date
        self.strategy_name = strategy_name

    def run(self):
        try:
            # 백테스팅 실행 (시간이 오래 걸리는 작업)
            success = self.backtester.run_backtest(self.codes, self.start_date, self.end_date, self.strategy_name)
            self.finished_signal.emit(self.backtester, success)
        except Exception as e:
            self.error_signal.emit(str(e))

class BacktestManager:
    """백테스팅 관리 매니저"""
    
    def __init__(self, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
        self.worker = None  # 워커 스레드 참조 유지
    
    def load_backtest_strategies(self):
        """백테스팅 탭의 전략 콤보박스 로드"""
        try:
            self.parent.backtest_tab.bt_strategy_combo.clear()
            
            # 설정 파일에서 전략 로드
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            strategies = []
            if config.has_section('STRATEGIES'):
                # 섹션의 모든 키에 대한 값을 가져옴 (예: stg_1 = 급등주 -> '급등주')
                for key in config['STRATEGIES']:
                    strategies.append(config['STRATEGIES'][key])
            
            # 기본 전략 추가 (없으면)
            if '통합 전략' not in strategies:
                strategies.insert(0, '통합 전략')
            
            # 중복 제거 (순서 유지)
            strategies = list(dict.fromkeys(strategies))
            
            self.parent.backtest_tab.bt_strategy_combo.addItems(strategies)
            
            # 마지막 선택 전략 설정 (SETTINGS -> last_strategy)
            if config.has_option('SETTINGS', 'last_strategy'):
                last_strategy = config.get('SETTINGS', 'last_strategy')
                index = self.parent.backtest_tab.bt_strategy_combo.findText(last_strategy)
                if index >= 0:
                    self.parent.backtest_tab.bt_strategy_combo.setCurrentIndex(index)
            
            self.logger.debug(f"백테스팅 전략 로드 완료: {len(strategies)}개")
            
        except Exception as ex:
            self.logger.error(f"백테스팅 전략 로드 실패: {ex}")

    def load_db_period(self):
        """DB 데이터 기간 및 종목 목록 조회 및 UI 설정"""
        try:
            backtester = KiwoomBacktester(db_path='stock_data.db')
            start_date, end_date = backtester.get_db_data_range()
            
            if start_date and end_date:
                self.parent.backtest_tab.bt_start_date.setText(start_date)
                self.parent.backtest_tab.bt_end_date.setText(end_date)
                self.logger.info(f"DB 데이터 기간 로드: {start_date} ~ {end_date}")
            else:
                self.logger.warning("DB에 데이터가 없거나 기간을 조회할 수 없습니다.")
            
            # 종목 목록 로드
            codes = backtester.get_all_stock_codes()
            self.parent.backtest_tab.bt_stock_combo.clear()
            self.parent.backtest_tab.bt_stock_combo.addItem("전체 종목")
            if codes:
                self.parent.backtest_tab.bt_stock_combo.addItems(codes)
                self.logger.info(f"DB 종목 목록 로드 완료: {len(codes)}개")
                
        except Exception as ex:
            self.logger.error(f"DB 정보 로드 실패: {ex}")
    
    def run_backtest(self):
        """백테스팅 실행 (비동기)"""
        try:
            # 중복 실행 방지
            if self.worker and self.worker.isRunning():
                self.logger.warning("이미 백테스팅이 진행 중입니다.")
                return

            # 1. KiwoomBacktester 인스턴스 생성 (기간 조회를 위해 먼저 생성)
            backtester = KiwoomBacktester(db_path='stock_data.db')
    
            # 2. DB에서 실제 데이터 기간 자동 조회
            start_date, end_date = backtester.get_db_data_range()
            if not start_date or not end_date: # type: ignore
                self.logger.warning("DB에서 백테스팅을 위한 데이터 기간을 찾을 수 없습니다.")
                return

            # 3. UI에 조회된 기간 설정 및 파라미터 가져오기
            self.parent.backtest_tab.bt_start_date.setText(start_date)
            self.parent.backtest_tab.bt_end_date.setText(end_date)
            initial_cash = int(self.parent.backtest_tab.bt_initial_cash.text())
            strategy_name = self.parent.backtest_tab.bt_strategy_combo.currentText()
            backtester.initial_cash = initial_cash # 초기 자금 설정

            if not all([start_date, end_date, strategy_name]): # type: ignore
                self.logger.warning("백테스팅 입력 오류: 시작일, 종료일, 투자 전략을 모두 선택해주세요.")
                return

            self.logger.info(f"백테스팅 시작 요청: {strategy_name} ({start_date} ~ {end_date})") # type: ignore
            self.parent.backtest_tab.bt_results_text.clear()
            self.parent.backtest_tab.bt_results_text.append(f"백테스팅을 시작합니다: {strategy_name}\n")
            self.parent.backtest_tab.bt_results_text.append("데이터 로딩 및 분석 중... 잠시만 기다려주세요.\n(데이터 양에 따라 시간이 소요될 수 있습니다)\n")
            
            # UI 비활성화
            self.parent.backtest_tab.bt_run_button.setEnabled(False)
            self.parent.backtest_tab.bt_run_button.setText("진행 중...")
            QApplication.processEvents()

            # 4. 백테스팅 대상 종목 가져오기
            selected_stock = self.parent.backtest_tab.bt_stock_combo.currentText()
            
            if selected_stock == "전체 종목":
                codes = backtester.get_all_stock_codes()
            else:
                codes = [selected_stock]
                
            if not codes:
                self.logger.warning("백테스팅 오류: 대상 종목이 없습니다.")
                self.parent.backtest_tab.bt_run_button.setEnabled(True)
                self.parent.backtest_tab.bt_run_button.setText("백테스팅 실행")
                return

            self.parent.backtest_tab.bt_results_text.append(f"대상 종목: {len(codes)}개 종목\n")
            QApplication.processEvents()
            
            # 5. 워커 스레드 생성 및 실행
            self.worker = BacktestWorker(backtester, codes, start_date, end_date, strategy_name)
            self.worker.finished_signal.connect(self.on_backtest_finished)
            self.worker.error_signal.connect(self.on_backtest_error)
            self.worker.start()

        except Exception as ex:
            self.logger.error(f"백테스팅 실행 준비 실패: {ex}")
            self.parent.backtest_tab.bt_run_button.setEnabled(True)
            self.parent.backtest_tab.bt_run_button.setText("백테스팅 실행")
            # QMessageBox.critical(self.parent, "백테스팅 오류", f"백테스팅 실행 중 오류가 발생했습니다:\n{ex}")

    def on_backtest_finished(self, backtester, success):
        """백테스팅 완료 처리"""
        try:
            strategy_name = self.worker.strategy_name
            
            # UI 복구
            self.parent.backtest_tab.bt_run_button.setEnabled(True)
            self.parent.backtest_tab.bt_run_button.setText("백테스팅 실행")
            
            if success and strategy_name in backtester.results:
                result = backtester.results[strategy_name]
                summary = (
                    f"총 수익률: {result['total_return']:.2f}%\n"
                    f"최종 자산: {result['final_value']:,.0f}원\n"
                    f"승률: {result['win_rate']:.2f}%\n"
                    f"총 거래 수: {result['total_trades']}\n"
                    f"최대 낙폭: {result['max_drawdown']:.2f}%"
                )
                self.parent.backtest_tab.bt_results_text.append("\n=== 백테스팅 결과 ===\n" + summary)
                backtester.plot_results(strategy_name)
                backtester.export_results(strategy_name)
                self.logger.info("백테스팅 완료 및 결과 표시 성공")
            else:
                self.parent.backtest_tab.bt_results_text.append("\n백테스팅 실행에 실패했거나 결과가 없습니다.")
                self.logger.warning("백테스팅 실패 또는 결과 없음")
                
        except Exception as ex:
            self.logger.error(f"백테스팅 결과 처리 중 오류: {ex}")
            self.parent.backtest_tab.bt_results_text.append(f"\n결과 처리 중 오류 발생: {ex}")

    def on_backtest_error(self, error_msg):
        """백테스팅 에러 처리"""
        self.logger.error(f"백테스팅 중 오류 발생: {error_msg}")
        self.parent.backtest_tab.bt_results_text.append(f"\n❌ 백테스팅 중 오류 발생: {error_msg}")
        
        # UI 복구
        self.parent.backtest_tab.bt_run_button.setEnabled(True)
        self.parent.backtest_tab.bt_run_button.setText("백테스팅 실행")


class AccountManager:
    """계좌 조회 및 잔고 관리 매니저"""
    def __init__(self, parent: 'MyWindow'):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
    
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
                    self.logger.debug("✅ 예수금상세현황 조회 성공") # type: ignore
                    parent._display_deposit_info(deposit_data)
                else:
                    self.logger.warning("⚠️ 예수금상세현황 조회 실패")
            except Exception as deposit_ex:
                self.logger.error(f"❌ 예수금상세현황 조회 실패: {deposit_ex}")

            # 2. REST API 잔고조회 (kt00004) - 초기 보유종목 확인
            self.logger.debug("🔍 계좌 잔고 조회 중...")
            try:
                balance_data = await parent.trader.client.get_acnt_balance()
                if balance_data:
                    # 키움 API 공식 문서 기준 필드명 사용
                    # stk_acnt_evlt_prst: 종목별계좌평가현황 (LIST)
                    holdings = balance_data.get('stk_acnt_evlt_prst', balance_data.get('output1', []))
                    
                    if holdings and len(holdings) > 0:
                        self.logger.info(f"📦 보유 종목 수: {len(holdings)}개")
                        self.logger.info("📋 보유 종목 목록 (REST API)") # type: ignore
                        
                        for stock in holdings:
                            # 키움 API 공식 문서 기준 필드명 (구 버전 호환)
                            raw_code = stock.get('stk_cd', stock.get('pdno', '알 수 없음'))
                            stock_code = parent.data_manager.normalize_stock_code(raw_code)  # A 접두사 제거
                            stock_name = stock.get('stk_nm', stock.get('prdt_name', '알 수 없음'))
                            quantity = parent.data_manager.safe_int(stock.get('rmnd_qty', stock.get('hldg_qty', 0)))
                            current_price = parent.data_manager.safe_int(stock.get('cur_prc', stock.get('prpr', 0)))
                            average_price = parent.data_manager.safe_int(stock.get('avg_prc', stock.get('pchs_avg_pric', 0)))

                            # 수수료와 세금을 반영한 실질 손익 계산
                            commission_rate = parent.trader.commission_rate if hasattr(parent, 'trader') else 0.00015
                            tax_rate = parent.trader.tax_rate if hasattr(parent, 'trader') else 0.0018

                            buy_cost_per_share = average_price * (1 + commission_rate)
                            sell_revenue_per_share = current_price * (1 - commission_rate - tax_rate)
                            net_profit_per_share = sell_revenue_per_share - buy_cost_per_share

                            profit_loss = net_profit_per_share * quantity
                            total_buy_cost = buy_cost_per_share * quantity
                            profit_rate = (profit_loss / total_buy_cost) * 100 if total_buy_cost > 0 else 0.0
                        
                        # 보유종목을 모니터링과 보유종목 리스트에 추가
                        for stock in holdings:
                            raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                            stock_code = parent.data_manager.normalize_stock_code(raw_code)  # A 접두사 제거
                            stock_name = stock.get('stk_nm', stock.get('prdt_name', ''))
                            quantity = parent.data_manager.safe_int(stock.get('rmnd_qty', stock.get('hldg_qty', 0)))
                            
                            if stock_code and quantity > 0:
                                # 모니터링 리스트에 추가
                                monitoring_exists = False
                                for i in range(parent.monitoringBox.count()):
                                    item_text = parent.monitoringBox.item(i).text()
                                    # 종목코드 추출 (종목명 유무와 관계없이)                                   
                                    if item_text == stock_code:
                                        monitoring_exists = True
                                        break
                                
                                if not monitoring_exists:
                                    parent.monitoring_manager.add_stock_to_monitoring(stock_code, None)
                                    self.logger.debug(f"   ✅ 모니터링 추가 (동기): {stock_code} ({stock_name})")
                                
                                # 보유종목 리스트에 추가
                                holding_exists = False
                                for i in range(parent.boughtBox.count()):
                                    item_text = parent.boughtBox.item(i).text()
                                    # 종목코드 추출 (종목명 유무와 관계없이)                                    
                                    if item_text == stock_code:
                                        holding_exists = True
                                        break
                                
                                if not holding_exists:
                                    parent.trading_tab.boughtBox.addItem(stock_code)
                                    self.logger.debug(f"   ✅ 보유종목 추가: {stock_code} ({stock_name})")                       
                        
                        # REST API 잔고 데이터를 웹소켓 balance_data에 저장 (중요!)
                        self._initialize_balance_data_from_rest_api(holdings)
                        
                        # 투자현황표 직접 업데이트는 _initialize_balance_data_from_rest_api 내부에서 수행됨
                        
                    else:
                        self.logger.info("📦 현재 보유 종목이 없습니다.")
                else:
                    self.logger.warning("⚠️ 계좌 잔고 조회 실패 또는 보유종목 없음")
                    
            except Exception as balance_ex:
                self.logger.error(f"계좌 잔고 조회 실패: {balance_ex}", exc_info=True)
                
                
        except Exception as ex:
            self.logger.error(f"계좌 잔고 조회 실패: {ex}", exc_info=True)
            

    async def handle_acnt_balance_query_async(self):
        """계좌 잔고조회 (비동기 버전) - post_login_setup에서 사용"""
        try:
            self.logger.debug("🔧 계좌 잔고 조회 시작 (비동기)")
            
            if not hasattr(self.parent, 'trader') or not self.parent.trader:
                self.logger.warning("⚠️ 트레이더가 초기화되지 않았습니다")
                return
            
            # 1. 예수금상세현황 조회
            try:
                deposit_data = await self.parent.trader.client.get_deposit_detail()
                if deposit_data and 'entr' in deposit_data:
                    entr_amount = self.parent.data_manager.safe_int(deposit_data.get('entr', 0))
                    self.logger.info(f"예수금: {entr_amount:,}원")
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

                    self.logger.info(f"총매입금액: {total_purchase_amount:,}원")
                    self.logger.info(f"총평가손익금액: {total_eval_pl_amount:+,}원")
                    self.logger.info(f"총수익률: {total_profit_rate:.2f}%")

                    # 종목별 평가손익 및 수익률 로깅
                    stock_list = eval_status.get('acnt_evlt_remn_indv_tot', [])
                    if stock_list:
                        for stock in stock_list:
                            stock_name = stock.get('stk_nm', 'N/A')
                            eval_profit = self.parent.data_manager.safe_int(stock.get('evltv_prft', 0))
                            profit_rate = self.parent.data_manager.safe_float(stock.get('prft_rt', 0.0))
                            self.logger.info(f"- {stock_name}: 평가손익 {eval_profit:+,}원, 수익률 {profit_rate:.2f}%")
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
                logging.error(f"계좌 잔고 조회 실패 (비동기): {balance_ex}", exc_info=True)
                
        except Exception as ex:
            logging.error(f"계좌 잔고 조회 실패 (비동기): {ex}", exc_info=True)

    
    async def _initialize_balance_data_from_rest_api(self, holdings):
        """REST API 잔고 데이터를 웹소켓 balance_data 형식으로 변환하고 투자현황표 업데이트"""
        
        parent = self.parent
        
        try:
            logging.debug("🔧 REST API 잔고 데이터를 웹소켓 balance_data 형식으로 변환 중...")
            
            # 웹소켓 클라이언트 확인
            if not hasattr(parent, 'login_handler') or not parent.login_handler:
                logging.warning("⚠️ login_handler 객체가 없습니다 - 데이터를 임시 저장합니다") # type: ignore
                parent._pending_balance_data = holdings
                return
            
            if not hasattr(parent.login_handler, 'websocket_client') or not parent.login_handler.websocket_client:
                logging.warning("⚠️ websocket_client가 없습니다 - 데이터를 임시 저장하고 웹소켓 준비 후 다시 시도합니다")
                parent._pending_balance_data = holdings
                return
            
            ws_client = parent.login_handler.websocket_client
            if not hasattr(ws_client, 'balance_data'):
                logging.warning("⚠️ ws_client.balance_data가 없습니다 - 데이터를 임시 저장합니다")
                parent._pending_balance_data = holdings
                return
            
            # REST API 데이터를 웹소켓 balance_data 형식으로 변환
            converted_count = 0
            for stock in holdings: # type: ignore
                stock_code = '알수없음'  # 예외 핸들링을 위한 기본값
                try:
                    # REST API 필드명 매핑
                    raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                    stock_code = parent.data_manager.normalize_stock_code(raw_code)  # A 접두사 제거
                    stock_name = stock.get('stk_nm', stock.get('prdt_name', ''))
                    quantity = parent.data_manager.safe_int(stock.get('rmnd_qty', stock.get('hldg_qty', 0)))
                    current_price = parent.data_manager.safe_int(stock.get('cur_prc', stock.get('prpr', 0)))
                    average_price = parent.data_manager.safe_int(stock.get('avg_prc', stock.get('pchs_avg_pric', 0)))                    
                    
                    if stock_code and quantity > 0:
                        # 수수료와 세금을 반영한 실질 손익 계산
                        commission_rate = parent.trader.commission_rate
                        tax_rate = parent.trader.tax_rate

                        buy_cost_per_share = average_price * (1 + commission_rate)
                        sell_revenue_per_share = current_price * (1 - commission_rate - tax_rate)
                        net_profit_per_share = sell_revenue_per_share - buy_cost_per_share

                        profit_loss = net_profit_per_share * quantity
                        total_buy_cost = buy_cost_per_share * quantity
                        profit_rate = (profit_loss / total_buy_cost) * 100 if total_buy_cost > 0 else 0.0

                        # UI 표시용 평가금액 및 매입금액 (수수료/세금 미포함)
                        evaluation_amount = quantity * current_price
                        purchase_amount = quantity * average_price
                        
                        # 웹소켓 balance_data 형식으로 저장
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
                            'order_available_qty': quantity,  # REST API에는 별도 필드가 없어 보유수량 사용
                            'total_purchase': purchase_amount,
                            'daily_net_buy': 0,  # REST API에는 당일 정보가 없음
                            'daily_total_profit': 0,
                            'daily_realized_profit': 0,
                            'daily_realized_profit_rate': 0,
                            'updated_at': datetime.now().isoformat()
                        }
                        
                        # trader.holdings에도 추가 (프로그램 시작 시 기존 보유 종목 동기화)
                        if hasattr(parent, 'trader') and parent.trader:
                            if stock_code not in parent.trader.holdings: # type: ignore
                                parent.trader.holdings[stock_code] = {'quantity': quantity}
                                # 매입 가격 및 시간 설정
                                if stock_code not in parent.trader.buy_prices:
                                    parent.trader.buy_prices[stock_code] = average_price
                                if stock_code not in parent.trader.buy_times:
                                    # REST API에는 매입 시간이 없으므로 현재 시간 사용
                                    parent.trader.buy_times[stock_code] = datetime.now()
                                self.logger.debug(f"   ✅ {stock_code} trader.holdings에 추가 (초기 로드, 수량: {quantity}주, 매입단가: {average_price}원)")
                            else:
                                # 이미 있는 경우 수량 업데이트
                                parent.trader.holdings[stock_code]['quantity'] = quantity
                                if stock_code not in parent.trader.buy_prices or parent.trader.buy_prices[stock_code] == 0:
                                    parent.trader.buy_prices[stock_code] = average_price
                        
                        # 모니터링 및 보유종목 리스트에 추가
                        if hasattr(parent, 'monitoring_manager'):
                            await parent.monitoring_manager.add_stock_to_monitoring_async(stock_code)
                        
                        if hasattr(parent.trading_tab, 'boughtBox'):
                            holding_exists = False
                            for i in range(parent.trading_tab.boughtBox.count()):
                                if stock_code in parent.trading_tab.boughtBox.item(i).text():
                                    holding_exists = True
                                    break
                            if not holding_exists:
                                parent.trading_tab.boughtBox.addItem(f"{stock_code}")
                                self.logger.debug(f"   ✅ 보유종목 UI 추가 (초기 로드): {stock_code}")
                        
                        converted_count += 1
                        
                        self.logger.debug(f"   ✅ {stock_code} 변환 완료: {quantity}주, {current_price:,}원")
                        
                except Exception as item_ex:
                    self.logger.error(f"❌ 종목 데이터 변환 실패 ({stock_code}): {item_ex}")
                    continue
            
            self.logger.info(f"✅ REST API 잔고 데이터 변환 완료: {converted_count}개 종목")
            
            # 웹소켓 balance_data에 저장 완료
            self.logger.debug(f"✅ 웹소켓 balance_data에 {converted_count}개 종목 저장 완료")
            self.logger.debug(f"   저장된 종목 목록: {list(ws_client.balance_data.keys())}")
            
            # 투자현황표 업데이트 (웹소켓 balance_data 사용)
            if converted_count > 0:
                self.logger.debug("🔧 투자 현황표 업데이트 시작 (REST API 잔고 → 웹소켓 balance_data)") # type: ignore
                parent.update_stock_table()
            
        except Exception as ex:
            self.logger.error(f"❌ REST API 잔고 데이터 변환 실패: {ex}", exc_info=True)


class ConditionSearchManager:
    """조건검색 관리 매니저"""
    
    def __init__(self, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
        self.is_manual_change = False # 사용자의 수동 변경 여부 플래그

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
            if not hasattr(self.parent.login_handler, 'websocket_client') or not self.parent.login_handler.websocket_client:
                self.logger.warning("⚠️ 웹소켓 클라이언트가 연결되지 않았습니다")
                return

            # 웹소켓을 통한 조건검색 목록조회
            try:
                await self.parent.login_handler.websocket_client.send_message({
                    'trnm': 'CNSRLST', # TR명
                })
                logging.debug("✅ 조건검색 목록조회 요청 전송 완료 (웹소켓)") # type: ignore

                # 웹소켓 응답은 receive_messages에서 처리됨
                logging.debug("💾 조건검색 목록조회 요청 완료 - 응답은 웹소켓에서 처리됩니다")

            except Exception as websocket_ex:
                self.logger.error(f"❌ 조건검색 목록조회 웹소켓 요청 실패: {websocket_ex}")
                self.parent.condition_search_list = None
        except Exception as ex:
            self.logger.error(f"❌ 조건검색 목록조회 실패: {ex}")
            self.parent.condition_search_list = None
  
    def check_and_auto_execute_saved_condition(self):
        """저장된 조건검색식이 있는지 확인하고 자동 실행"""
        # 사용자가 수동으로 전략을 변경한 경우에는 자동 실행을 건너뜁니다.
        if self.is_manual_change:
            self.logger.debug("사용자 수동 변경으로 인해 저장된 조건검색식 자동 실행을 건너뜁니다.")
            self.is_manual_change = False # 플래그 초기화
            return False

        try:
            
            # settings.ini에서 저장된 전략 확인
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            if config.has_option('SETTINGS', 'last_strategy'):
                last_strategy = config.get('SETTINGS', 'last_strategy')
                self.logger.debug(f"📋 저장된 전략 확인: {last_strategy}")
                
                # 저장된 전략이 조건검색식인지 확인 (조건검색 목록에 있는지 확인)
                if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                    condition_names = [condition['title'] for condition in self.parent.condition_search_list]
                    if last_strategy in condition_names:
                        self.logger.info(f"🔍 저장된 조건검색식 발견: {last_strategy}")
                        
                        # 콤보박스에서 해당 조건검색식 찾아서 선택 (시그널 발생 방지)
                        combo = self.parent.trading_tab.comboStg
                        index = combo.findText(last_strategy)
                        if index != -1:
                            combo.blockSignals(True)
                            try:
                                combo.setCurrentIndex(index)
                                self.logger.debug(f"✅ 저장된 조건검색식 UI 선택: {last_strategy}")
                            finally:
                                combo.blockSignals(False)
                            
                            # 자동 실행 (1초 후)
                            async def delayed_condition_search():
                                await asyncio.sleep(1.0)  # 1초 대기
                                # stg_changed를 직접 호출하여 로직 일원화
                                await self.parent.strategy_manager.stg_changed()

                            asyncio.create_task(delayed_condition_search())
                            self.logger.debug("🔍 저장된 조건검색식 자동 실행 예약 (1초 후)")
                            self.logger.debug("📋 조건검색식이 자동으로 실행되어 모니터링 종목에 추가됩니다")
                            return True # 함수를 종료하여 불필요한 폴백 로직 방지
                            # 여기서 함수를 종료하지 않고 계속 진행하여 다른 초기화 로직이 실행되도록 함
                
                # 통합 전략인 경우 모든 조건검색식 실행
                if last_strategy == "통합 전략":
                    self.logger.debug(f"🔍 저장된 통합 전략 발견: {last_strategy}")
                     # 콤보박스에서 통합 전략 찾기
                    index = self.parent.trading_tab.comboStg.findText(last_strategy)
                    if index >= 0:
                        # 통합 전략 선택
                        self.parent.trading_tab.comboStg.setCurrentIndex(index)
                        self.logger.debug(f"✅ 저장된 통합 전략 선택: {last_strategy}")
                        # 자동 실행 (1초 후)
                        async def delayed_integrated_search():
                            try:
                                await asyncio.sleep(1.5)
                                await self.parent.condition_search_manager.handle_integrated_condition_search()
                            except asyncio.CancelledError:
                                self.logger.debug("통합 조건검색 태스크 취소됨")
                            except Exception as e:
                                self.logger.error(f"통합 조건검색 실행 실패: {e}")
                        task = asyncio.create_task(delayed_integrated_search())
                        self.parent._delayed_search_task = task
                        self.logger.debug("🔍 저장된 통합 전략 자동 실행 예약 (1.5초 후)")
                        return True  # 함수 종료

                # 일반 조건검색식인 경우
                elif hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                    condition_names = [condition['title'] for condition in self.parent.condition_search_list]
                    if last_strategy in condition_names:
                        self.logger.debug(f"🔍 저장된 조건검색식 발견: {last_strategy}")
                        index = self.parent.trading_tab.comboStg.findText(last_strategy)
                        if index >= 0:
                            self.parent.trading_tab.comboStg.setCurrentIndex(index)
                            self.logger.debug(f"✅ 저장된 조건검색식 선택: {last_strategy}")
                            async def delayed_search():
                                await asyncio.sleep(1.5)
                                await self.parent.condition_search_manager.handle_condition_search()
                            asyncio.create_task(delayed_search())
                            self.logger.debug(f"🔍 저장된 조건검색식 자동 실행 예약 (1.5초 후)")
                            return True # 함수 종료
                else:
                    self.logger.debug(f"📋 저장된 전략이 조건검색식이 아닙니다: {last_strategy}")
                    self.logger.debug("📋 일반 투자전략이 선택되어 있습니다")
                    return False  # 조건검색식이 아님
            else:
                self.logger.debug("📋 저장된 전략이 없습니다")
                self.logger.debug("📋 투자전략 콤보박스에서 원하는 전략을 선택하세요")
                return False  # 저장된 전략이 없음
            
        except Exception as ex:
            self.logger.error(f"❌ 저장된 조건검색식 확인 및 자동 실행 실패: {ex}", exc_info=True)
            return False  # 오류 발생
    
    async def handle_integrated_condition_search(self):
        """통합 전략 실행: 모든 조건검색식 순차적으로 실행"""
        try:
            if not hasattr(self.parent, 'condition_search_list') or not self.parent.condition_search_list:
                self.logger.warning("⚠️ 조건검색 목록이 없어 통합 검색을 실행할 수 없습니다.")
                return

            self.logger.info(f"🔄 통합 조건검색 시작: {len(self.parent.condition_search_list)}개 조건식 실행")

            for condition in self.parent.condition_search_list:
                seq = condition.get('seq')
                name = condition.get('title')
                if seq and name:
                    self.logger.debug(f"  - 조건검색 실행: {name} (seq: {seq})")
                    # 각 조건검색을 순차적으로 실행하고, API 제한을 피하기 위해 약간의 지연을 둡니다.
                    await self.parent.start_condition_realtime(seq, name)
                    await asyncio.sleep(1) # API 요청 간격

            self.logger.debug("✅ 모든 조건검색식에 대한 실시간 모니터링이 시작되었습니다.")

        except Exception as ex:
            self.logger.error(f"❌ 통합 조건검색 실행 실패: {ex}")

    async def handle_condition_search(self):
        """조건검색 실행 (웹소켓 기반)"""
        try:
            if self.parent.trading_tab.comboStg.currentText():
                condition_name = self.parent.trading_tab.comboStg.currentText()
                condition_seq = next((item['seq'] for item in self.parent.condition_search_list if item['title'] == condition_name), None) # type: ignore
                if condition_seq is not None: # type: ignore
                    self.logger.info(f"🔍 조건검색 시작: {condition_name} (seq: {condition_seq})") # type: ignore
                    await self.parent.execute_condition_search(condition_seq, condition_name) # type: ignore
        except Exception as ex:
            self.logger.error(f"조건검색 실행 실패: {ex}", exc_info=True)


