import logging
from utils import create_fire_and_forget_task
import asyncio
import threading


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
                        create_fire_and_forget_task(ws_client.subscribe_stock_execution_data([code], 'monitoring'))
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
            # 보유 중인 종목인지 확인
            is_held = False
            if hasattr(self.parent, 'boughtBox'):
                for i in range(self.parent.boughtBox.count()):
                    if code in self.parent.boughtBox.item(i).text():
                        is_held = True
                        break
            
            # 보유 중인 종목이면 제거하지 않음 (UI 및 캐시 모두 유지)
            if is_held:
                self.logger.info(f"🛡️ {code}는 보유 중인 종목이므로 모니터링 목록에서 제거하지 않습니다.")
                return True
            
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
                        create_fire_and_forget_task(ws_client.unsubscribe_stock_execution_data([code]))
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
        모니터링 박스 및 보유 종목 박스에서 종목 코드 리스트 추출 (통합 버전)
        
        다양한 형식의 아이템 텍스트를 파싱하여 종목코드만 추출:
        - "종목코드 - 종목명" 형식
        - "종목코드 종목명" 형식 (공백 구분)
        - "종목코드" 단독
        
        Returns:
            list: 종목코드 리스트
        """
        try:
            stock_codes = set() # 중복 제거를 위해 set 사용
            
            # 1. 모니터링 박스에서 추출
            if hasattr(self.parent, 'trading_tab') and hasattr(self.parent.trading_tab, 'monitoringBox'):
                monitoring_box = self.parent.trading_tab.monitoringBox
                for i in range(monitoring_box.count()):
                    item = monitoring_box.item(i)
                    if not item: continue
                    item_text = item.text().strip()
                    if not item_text: continue
                    
                    code = item_text.split()[0] # 첫 번째 공백 앞부분 사용 (종목코드)
                    
                    # 'A' 접두사 제거
                    if code.startswith('A'): code = code[1:]
                    
                    # 6자리 종목코드만 허용
                    if code and code.isdigit() and len(code) == 6:
                        stock_codes.add(code)

            # 2. 보유 종목 박스에서 추출 (보유 종목도 차트 업데이트 필요)
            if hasattr(self.parent, 'boughtBox'):
                bought_box = self.parent.boughtBox
                for i in range(bought_box.count()):
                    item = bought_box.item(i)
                    if not item: continue
                    item_text = item.text().strip()
                    if not item_text: continue
                    
                    code = item_text.split()[0]
                    
                    if code.startswith('A'): code = code[1:]
                    
                    if code and code.isdigit() and len(code) == 6:
                        stock_codes.add(code)
            
            result_list = list(stock_codes)
            self.logger.debug(f"모니터링+보유 종목 코드 추출: {len(result_list)}개 - {result_list}")
            return result_list
            
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
                        create_fire_and_forget_task(websocket_client.subscribe_stock_execution_data([code], 'monitoring'))
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
                        create_fire_and_forget_task(websocket_client.unsubscribe_stock_execution_data([code]))
                        self.logger.debug(f"📡 실시간 체결 데이터 구독 해제: {code}")
                    except RuntimeError:
                        self.logger.warning("⚠️ 이벤트 루프가 없어 구독 해제를 건너뜁니다")
                else:
                    self.logger.warning(f"⚠️ 웹소켓이 연결되지 않아 실시간 구독 해제를 할 수 없습니다: {code}")
            else:
                self.logger.warning(f"⚠️ 웹소켓 클라이언트가 없어 실시간 구독 해제를 할 수 없습니다: {code}")
                
        except Exception as ex:
            self.logger.error(f"❌ 실시간 체결 데이터 구독 해제 실패: {code} - {ex}")

