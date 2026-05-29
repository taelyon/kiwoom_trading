"""
키움 REST API 기반 Headless CLI 자동매매 프로그램 진입점
"""

import sys
import logging
import os
import asyncio
import traceback
import signal
from threading import Lock
from datetime import datetime

from utils import setup_logging, _prevent_system_sleep, create_fire_and_forget_task
from database import AsyncDatabaseManager
from trader import KiwoomTrader, AutoTrader
from strategy import KiwoomStrategy
from kiwoom_rest import KiwoomRestClient
from kiwoom_websocket import KiwoomWebSocketClient

from core_managers import (LoginHandler, DataManager, MonitoringManager, StrategyManager, 
                         TradingManager, AccountManager, ConditionSearchManager, MLManager)
from chart_manager import ChartDataCache

class TradingApp:
    """메인 애플리케이션 클래스 (Headless CLI 전용)"""
    
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # 기본 변수 초기화
        self.is_loading_strategy = False
        self.market_close_emitted = False
        
        # 객체 초기화 (트레이더는 API 연결 후 생성)
        self.trader = None
        self.objstg = None
        self.autotrader = None
        self.chart_cache = None  # 차트 데이터 캐시
        
        # 조건검색 관련 변수
        self.condition_search_list = []  # 조건검색 목록
        self.active_realtime_conditions = set()  # 활성화된 실시간 조건검색
        self.condition_search_results = {}  # 조건검색 결과 저장
        self.stock_condition_map = {}  # 종목별 조건검색 이름 매핑 (종목코드: 조건검색 이름)
        self.current_condition_name = None  # 현재 실행 중인 조건검색 이름 (응답 처리용)
        self.chart_drawing_lock = Lock()
        
        # 비동기 작업을 위한 Lock
        self.trading_lock = asyncio.lock = asyncio.Lock()
        
        # post_login_setup을 한 번만 실행하기 위한 플래그
        self._post_login_setup_done = False
        
        # 매니저 객체 초기화 (Pure Python 클래스들)
        self.data_manager = DataManager(self)
        self.monitoring_manager = MonitoringManager(self)
        self.strategy_manager = StrategyManager(self)
        self.trading_manager = TradingManager(self)
        self.account_manager = AccountManager(self)
        self.condition_search_manager = ConditionSearchManager(self)
        self.ml_manager = MLManager(self)
        
        # 로그인 핸들러 생성
        self.login_handler = LoginHandler(self)
        self.login_handler.load_settings_sync()

    async def start(self):
        """애플리케이션 비동기 구동 시작 및 자동 로그인 시도"""
        try:
            self.logger.info("🚀 Headless TradingApp 구동 시작...")
            _prevent_system_sleep()  # 절전 모드 방지
            
            # 자동 연결 설정 확인 후 API 연결
            if self.login_handler.config.getboolean('LOGIN', 'autoconnect', fallback=False):
                if not self._post_login_setup_done:
                    self.logger.info("🔑 자동 연결 시도 중...")
                    await self.login_handler.handle_api_connection()
                    await self.login_handler.start_websocket_client()
                    # post_login_setup은 웹소켓 로그인 성공 통지(콜백) 후 시점에 자동으로 실행되도록 위임함
            else:
                self.logger.warning("⚠️ 자동 연결 설정(autoconnect)이 비활성화되어 있습니다. 웹 대시보드에서 수동 연결이 필요합니다.")
                
        except Exception as ex:
            self.logger.error(f"❌ TradingApp 구동 실패: {ex}", exc_info=True)

    def update_connection_status(self, is_connected: bool):
        """API 연결 상태 업데이트 콜백"""
        self.logger.info(f"📡 API 연결 상태 변경 통지 수신: {'연결됨' if is_connected else '연결 해제됨'}")
        self.update_stock_table()

    def show_message_box(self, title, message):
        """메시지 표시 헬퍼 (CLI용 로그 대체)"""
        self.logger.warning(f"💬 알림: [{title}] {message}")

    async def _shutdown_async(self):
        """비동기 리소스 정리 및 안전 종료"""
        self.logger.info("🔄 애플리케이션 비동기 종료 절차를 시작합니다...")
        
        # 0. 차트 데이터 수집 태스크 취소
        if self.chart_cache and hasattr(self.chart_cache, 'active_chart_tasks'):
            active_tasks = list(self.chart_cache.active_chart_tasks.values())
            if active_tasks:
                self.logger.debug(f"🔌 진행 중인 {len(active_tasks)}개의 차트 수집 작업을 취소합니다.")
                for task in active_tasks:
                    task.cancel()
                try:
                    await asyncio.gather(*active_tasks, return_exceptions=True)
                except asyncio.CancelledError:
                    pass
                    
        # 1. 웹소켓 연결 해제
        if self.login_handler and getattr(self.login_handler, 'websocket_client', None):
            self.logger.info("🔌 웹소켓 연결 해제를 시도합니다...")
            await self.login_handler.websocket_client.stop()
 
        # 2. REST 클라이언트 연결 해제
        if self.login_handler and self.login_handler.kiwoom_client:
            self.logger.info("🔌 REST API 연결 해제를 시도합니다...")
            await self.login_handler.kiwoom_client.disconnect()
 
        # 3. 설정 저장
        if self.login_handler:
            self.logger.info("💾 설정 정보 저장 중...")
            await self.login_handler.save_settings()
 
        # 4. 모든 매매 및 스케줄러 중지
        if self.autotrader: 
            self.autotrader.stop_auto_trading()
        if self.chart_cache: 
            self.chart_cache.stop()
        if self.ml_manager:
            self.ml_manager.stop()
            
        self.logger.info("✅ 모든 비동기 리소스 정리 완료.")

    def extract_monitoring_stock_codes(self):
        """모니터링 종목 코드 추출 (MonitoringManager 위임)"""
        return self.monitoring_manager.extract_monitoring_stock_codes_enhanced()

    async def post_login_setup(self):
        """로그인 후 핵심 거래 관련 객체 초기화 및 백그라운드 태스크 기동"""
        if self._post_login_setup_done:
            self.logger.debug("이미 초기화 및 설정이 완료되었습니다.")
            return

        try:
            self.logger.info("⚙️ 로그인 완료 후 거래 시스템 초기화 시작...")
            
            # 1. 차트 데이터 캐시 초기화
            if not self.chart_cache:
                self.chart_cache = ChartDataCache(self.trader, self)
                self.logger.debug("🔍 ChartDataCache 객체 생성 완료")
                
                # 웹 대시보드와 차트 업데이트 시그널 바인딩
                try:
                    from web_dashboard import on_chart_data_updated
                    self.chart_cache.data_updated.connect(on_chart_data_updated)
                    self.logger.debug("✅ ChartDataCache와 Web Dashboard 시그널 바인딩 완료")
                except Exception as sig_err:
                    self.logger.error(f"❌ Web Dashboard 시그널 바인딩 실패: {sig_err}")
            
            if hasattr(self.login_handler, 'kiwoom_client') and self.login_handler.kiwoom_client:
                self.login_handler.kiwoom_client.chart_cache = self.chart_cache
                self.logger.debug("🔍 chart_cache를 KiwoomRestClient에 설정 완료")
                
            # 2. 자동매매 객체 초기화 및 시작
            if not self.autotrader:
                self.autotrader = AutoTrader(self.trader, self)
                self.logger.debug("🔍 AutoTrader 객체 생성 완료")
                self.autotrader.start_auto_trading()
                self.logger.info(f"✅ 자동매매 루프 기동 완료 ({self.trader.evaluation_interval}초 주기)")

            # 3. 조건검색 목록조회 (웹소켓 기반)
            try:
                ws_client = getattr(self.login_handler, 'websocket_client', None)
                if ws_client and ws_client.connected:
                    await self.handle_condition_search_list_query()
                    self.logger.debug("✅ 조건검색 목록조회 완료 (웹소켓)")
            except Exception as condition_ex:
                self.logger.error(f"❌ 조건검색 목록조회 실패: {condition_ex}", exc_info=True)

            # 4. 전체 종목 코드 캐싱
            try:
                await self.data_manager._cache_all_stock_codes_async()
            except Exception as cache_ex:
                self.logger.error(f"❌ 전체 종목 코드 캐싱 실패: {cache_ex}", exc_info=True)

            # 5. 계좌 잔고조회 (즉시 실행)
            try:
                await self.account_manager.handle_acnt_balance_query_async()
                self.logger.debug("✅ 계좌 잔고조회 즉시 실행 완료 (비동기)")
            except Exception as balance_ex:
                self.logger.error(f"❌ 계좌 잔고조회 실행 실패: {balance_ex}", exc_info=True)

            # 6. 대기 중인 API 큐 처리
            try:
                if self.chart_cache and hasattr(self.chart_cache, 'api_request_queue'):
                    queue_size = len(self.chart_cache.api_request_queue)
                    if queue_size > 0:
                        self.logger.debug(f"🔧 대기 중인 API 큐 처리 시작: {queue_size}개 종목")
                        self.chart_cache._start_queue_processing()
            except Exception as queue_ex:
                self.logger.error(f"❌ API 큐 처리 시작 실패: {queue_ex}", exc_info=True)

            self._post_login_setup_done = True
            self.logger.info("✨ 거래 시스템 초기화 및 시작 처리가 성공적으로 완료되었습니다.")
            
        except Exception as ex:
            self.logger.error(f"❌ 로그인 후 초기화 실패: {ex}", exc_info=True)
            self.logger.debug("⚠️ 초기화 실패했지만 애플리케이션을 계속 가동합니다.")

    # --- 실시간 상태 통보용 껍데기/대시보드 통지 메서드 ---

    def update_stock_table(self):
        """투자 현황 갱신 이벤트 트리거 (실시간 브로드캐스트용)"""
        pass

    def update_acnt_balance_display(self, balance_data: dict):
        """잔고 정보 갱신 시 호출"""
        self.update_stock_table()

    def update_order_result(self, code: str, order_type: str, quantity: int, price: float, success: bool):
        """주문 결과 수신 시 호출"""
        self.update_stock_table()

    # --- 조건검색 관련 메서드 (ConditionSearchManager 위임) ---

    async def handle_condition_search_list_query(self):
        """조건검색 목록 조회"""
        await self.condition_search_manager.handle_condition_search_list_query()

    async def start_condition_realtime(self, seq, condition_name=None):
        """조건검색 실시간 모니터링 시작 (웹소켓)"""
        try:
            ws_client = getattr(self.login_handler, 'websocket_client', None)
            if not ws_client or not ws_client.connected:
                self.logger.error("❌ 웹소켓 클라이언트가 연결되지 않았습니다")
                return
            
            if condition_name:
                self.current_condition_name = condition_name
                self.logger.debug(f"🔍 조건검색 실시간 요청 시작 (웹소켓): {seq} ({condition_name})")
            else:
                self.logger.debug(f"🔍 조건검색 실시간 요청 시작 (웹소켓): {seq}")
            
            await ws_client.send_message({
                'trnm': 'CNSRREQ',
                'seq': seq,
                'search_type': '1',
                'stex_tp': 'K'
            })
            
        except Exception as ex:
            self.logger.error(f"❌ 조건검색 실시간 요청 실패: {ex}", exc_info=True)

    async def execute_condition_search(self, seq, name):
        """ConditionSearchManager의 실행 요청을 전달받음"""
        try:
            if seq is not None and name:
                await self.start_condition_realtime(seq, name)
        except Exception as ex:
            self.logger.error(f"조건검색 실행 위임 실패: {ex}", exc_info=True)

    async def stop_condition_realtime(self, seq):
        """조건검색 실시간 해제 (웹소켓)"""
        try:
            ws_client = getattr(self.login_handler, 'websocket_client', None)
            if not ws_client or not ws_client.connected:
                return
            
            await ws_client.send_message({
                'trnm': 'CNSRCLR',
                'seq': seq
            })
            self.logger.debug(f"✅ 조건검색 실시간 해제 전송 (웹소켓): {seq}")
        except Exception as ex:
            self.logger.error(f"❌ 조건검색 실시간 해제 실패: {ex}", exc_info=True)

    def remove_monitoring_stock(self, code):
        """모니터링 목록에서 종목 제거"""
        try:
            if self.chart_cache:
                self.chart_cache.remove_stock(code)
                self.logger.debug(f"🗑️ [{code}] 차트 데이터 캐시에서 제거됨")
        except Exception as ex:
            self.logger.error(f"모니터링 종목 제거 실패 ({code}): {ex}", exc_info=True)

async def main():
    """메인 실행 비동기 함수"""
    setup_logging()
    logging.info("🚀 Antigravity Kiwoom Headless CLI 프로그램 구동 시작")

    app = TradingApp()
    
    # 실시간 웹 대시보드 서버 구동 (8081 포트, 백그라운드 태스크)
    try:
        from web_dashboard import start_web_dashboard
        create_fire_and_forget_task(start_web_dashboard(app, host="0.0.0.0", http_port=8081, ws_port=8082))
        logging.info("🌐 실시간 Web Dashboard 백그라운드 태스크 기동 (Port: 8081)")
    except Exception as dashboard_err:
        logging.error(f"❌ 웹 대시보드 기동 실패: {dashboard_err}", exc_info=True)

    # TradingApp 시작
    await app.start()
    
    # 종료 시그널 핸들러 설정 (Graceful Shutdown)
    loop = asyncio.get_running_loop()
    
    stop_event = asyncio.Event()
    
    def shutdown_signal_handler():
        logging.info("🚨 종료 시그널 수신! 안전 종료 절차에 진입합니다...")
        stop_event.set()

    # Windows OS는 signal.SIGINT/SIGTERM 핸들러를 add_signal_handler로 등록할 수 없음
    # 따라서 try-except KeyboardInterrupt로 감싸거나, Windows가 아닌 경우에만 add_signal_handler 등록
    if sys.platform != 'win32':
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown_signal_handler)
            
    try:
        # 종료 이벤트 대기
        if sys.platform == 'win32':
            # Windows에서는 KeyboardInterrupt를 catch하기 위해 주기적으로 sleep 수행
            while not stop_event.is_set():
                await asyncio.sleep(0.5)
        else:
            await stop_event.wait()
            
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.info("⚠️ 키보드 인터럽트 또는 태스크 취소 발생")
    finally:
        # 리소스 안전 정리
        await app._shutdown_async()
        logging.info("👋 프로그램이 정상적으로 종료되었습니다.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.critical(f"메인 실행 중 치명적인 오류 발생: {e}", exc_info=True)
        sys.exit(1)