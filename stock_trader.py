"""
키움 REST API 기반 자동매매 프로그램
"""

import sys
import logging
import os
import asyncio
import traceback
from threading import Lock
from datetime import datetime

# pyqtgraph가 PyQt6를 사용하도록 환경변수 설정 (PyQt5 충돌 방지)
os.environ['PYQTGRAPH_QT_LIB'] = 'PyQt6'
os.environ['QT_API'] = 'pyqt6'
os.environ['QT_AUTO_SCREEN_SCALE_FACTOR'] = '1'
os.environ['QT_SCALE_FACTOR'] = '1'

import qasync
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox, QListWidgetItem, QWidget, QVBoxLayout, QTabWidget, QTableWidgetItem, QFrame
from PyQt6.QtCore import QTimer, pyqtSignal, Qt
from PyQt6.QtGui import QColor

from utils import setup_logging, _prevent_system_sleep
from database import AsyncDatabaseManager
from trader import KiwoomTrader, AutoTrader
from strategy import KiwoomStrategy
from kiwoom_api import KiwoomRestClient, KiwoomWebSocketClient

from ui_managers import (LoginHandler, DataManager, MonitoringManager, StrategyManager, 
                         TradingManager, BacktestManager, AccountManager, ConditionSearchManager)
from ui_widgets import TradingTabWidget, BacktestTabWidget
from chart_data import ChartDataCache

class MyWindow(QWidget):
    """메인 윈도우 클래스"""
    
    def __init__(self):
        super().__init__()
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
        self.trading_lock = asyncio.Lock()

        # Manager 초기화 (UI 생성 전에 초기화)
        self.data_manager = DataManager(self)
        self.monitoring_manager = MonitoringManager(self)
        self.strategy_manager = StrategyManager(self)
        self.trading_manager = TradingManager(self)
        self.backtest_manager = BacktestManager(self)
        self.account_manager = AccountManager(self)
        self.condition_search_manager = ConditionSearchManager(self)
        
        # UI 생성
        self.init_ui()

        # 로그인 핸들러 생성
        self.login_handler = LoginHandler(self)
        self.login_handler.load_settings_sync()

        # post_login_setup을 한 번만 실행하기 위한 플래그
        self._post_login_setup_done = False
        
        # LoginHandler의 시그널을 MyWindow의 UI 업데이트 메서드에 연결
        self.login_handler.connection_status_changed.connect(self.update_connection_ui)

        # 자동 연결 시도 (qasync 방식)
        asyncio.create_task(self.attempt_auto_connect())

    def show_message_box(self, title, message):
        """메시지 박스를 표시하는 슬롯"""
        self.logger.warning(f"팝업 메시지: [{title}] {message}") # 팝업 대신 로그로 출력
    
    def apply_modern_style(self):
        """현대적이고 눈에 피로하지 않은 스타일 적용"""
        style = """
        /* 전체 애플리케이션 스타일 */
        QWidget {
            background-color: #f5f5f5;
            color: #333333;
            font-family: 'Segoe UI', 'Malgun Gothic', sans-serif;
            font-size: 10pt;
        }
        
        /* 메인 윈도우 */
        QMainWindow {
            background-color: #f5f5f5;
        }
        
        /* 탭 위젯 */
        QTabWidget::pane {
            border: 1px solid #d0d0d0;
            background-color: #ffffff;
            border-radius: 4px;
        }
        
        QTabWidget::tab-bar {
            alignment: left;
        }
        
        QTabBar::tab {
            background-color: #e8e8e8;
            color: #555555;
            padding: 8px 16px;
            margin-right: 2px;
            border-top-left-radius: 4px;
            border-top-right-radius: 4px;
            border: 1px solid #d0d0d0;
            border-bottom: none;
        }
        
        QTabBar::tab:selected {
            background-color: #ffffff;
            color: #2c3e50;
            font-weight: bold;
            border-bottom: 1px solid #ffffff;
        }
        
        QTabBar::tab:hover {
            background-color: #f0f0f0;
        }
        
        /* 버튼 스타일 */
        QPushButton {
            background-color: #3498db;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
            font-weight: bold;
            min-height: 20px;
        }
        
        QPushButton:hover {
            background-color: #2980b9;
        }
        
        QPushButton:pressed {
            background-color: #21618c;
        }
        
        QPushButton:disabled {
            background-color: #bdc3c7;
            color: #7f8c8d;
        }
        
        /* 위험한 버튼 (매도, 삭제 등) */
        QPushButton[class="danger"] {
            background-color: #e74c3c;
        }
        
        QPushButton[class="danger"]:hover {
            background-color: #c0392b;
        }
        
        /* 성공 버튼 (매수, 연결 등) */
        QPushButton[class="success"] {
            background-color: #27ae60;
        }
        
        QPushButton[class="success"]:hover {
            background-color: #229954;
        }
        
        /* 입력 필드 */
        QLineEdit, QTextEdit, QComboBox {
            background-color: #ffffff;
            border: 2px solid #e0e0e0;
            border-radius: 4px;
            padding: 6px;
            color: #333333;
        }
        
        QLineEdit:focus, QTextEdit:focus, QComboBox:focus {
            border-color: #3498db;
        }
        
        /* 리스트 위젯 */
        QListWidget {
            background-color: #ffffff;
            border: 2px solid #e0e0e0;
            border-radius: 4px;
            alternate-background-color: #f8f9fa;
            selection-background-color: #3498db;
            selection-color: white;
        }
        
        QListWidget::item {
            padding: 6px;
            border-bottom: 1px solid #f0f0f0;
        }
        
        QListWidget::item:selected {
            background-color: #3498db;
            color: white;
        }
        
        QListWidget::item:hover {
            background-color: #ecf0f1;
            color: black;
        }
        
        /* 라벨 */
        QLabel {
            color: #2c3e50;
            font-weight: normal;
        }
        
        QLabel[class="title"] {
            font-size: 13pt;
            font-weight: bold;
            color: #2c3e50;
            padding: 4px 0px;
        }
        
        QLabel[class="status"] {
            font-weight: bold;
            padding: 4px 8px;
            border-radius: 4px;
        }
        
        /* 체크박스 */
        QCheckBox {
            color: #2c3e50;
            spacing: 8px;
        }
        
        QCheckBox::indicator {
            width: 16px;
            height: 16px;
            border: 2px solid #bdc3c7;
            border-radius: 3px;
            background-color: #ffffff;
        }
        
        QCheckBox::indicator:checked {
            background-color: #3498db;
            border-color: #3498db;
        }
        
        /* 그룹박스 */
        QGroupBox {
            font-weight: bold;
            color: #2c3e50;
            border: 2px solid #d0d0d0;
            border-radius: 6px;
            margin-top: 8px;
            padding-top: 10px;
            background-color: #ffffff;
        }
        
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 8px 0 8px;
            background-color: #ffffff;
        }
        
        /* 스크롤바 */
        QScrollBar:vertical {
            background-color: #f0f0f0;
            width: 12px;
            border-radius: 6px;
        }
        
        QScrollBar::handle:vertical {
            background-color: #bdc3c7;
            border-radius: 6px;
            min-height: 20px;
        }
        
        QScrollBar::handle:vertical:hover {
            background-color: #95a5a6;
        }
        
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0px;
        }
        
        /* 터미널 출력 영역 */
        QTextEdit[class="terminal"] {
            background-color: #2c3e50;
            color: #ecf0f1;
            border: 2px solid #34495e;
            border-radius: 4px;
            font-family: 'Consolas', 'Monaco', monospace;
            font-size: 10pt;
        }
        
        /* 상태 표시 */
        QLabel[class="connected"] {
            color: #27ae60;
            font-weight: bold;
        }
        
        QLabel[class="disconnected"] {
            color: #e74c3c;
            font-weight: bold;
        }
        
        /* 구분선 */
        QFrame[class="separator"] {
            color: #bdc3c7;
            background-color: #bdc3c7;
            max-height: 1px;
        }
        """
        self.setStyleSheet(style)

    def init_ui(self):
        """UI 초기화 (탭 구조)"""
        try:
            self.setWindowTitle("키움 REST API 자동매매 프로그램 v3.0")
            self.setGeometry(0, 0, 1900, 980)
            
            # 전체 애플리케이션 스타일 적용
            self.apply_modern_style()
            
            # ===== 메인 탭 위젯 생성 =====
            self.tab_widget = QTabWidget()
            
            # 탭 1: 실시간 매매
            self.trading_tab = TradingTabWidget(self)
            self.tab_widget.addTab(self.trading_tab, "실시간 매매")
            
            # 탭 2: 백테스팅
            self.backtest_tab = BacktestTabWidget(self)
            self.tab_widget.addTab(self.backtest_tab, "백테스팅")

            # TradingTabWidget이 생성된 후 전략 콤보박스 로드
            self.strategy_manager.load_strategy_combos()
            
            # BacktestTabWidget이 생성된 후 백테스팅 전략 콤보박스 로드
            self.backtest_manager.load_backtest_strategies()
            
            # 메인 레이아웃
            main_layout = QVBoxLayout()
            main_layout.addWidget(self.tab_widget)
            self.setLayout(main_layout)
            
            # 창 표시 안정성을 위한 설정
            self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint | Qt.WindowType.WindowCloseButtonHint)
            self.show()
            self.raise_()
            self.activateWindow()
            
        except Exception as ex:
            self.logger.error(f"UI 초기화 실패: {ex}")

    def closeEvent(self, event):
        """윈도우 종료 이벤트 핸들러"""
        self.logger.info("🔄 애플리케이션 종료 절차를 시작합니다...")

        # 1. pyqtgraph 관련 위젯을 먼저 정리
        try:
            if hasattr(self, 'trading_tab') and hasattr(self.trading_tab, 'realtime_chart_widget'):
                self.logger.debug("실시간 차트 위젯 정리 시도...")
                self.trading_tab.realtime_chart_widget.deleteLater()
                self.logger.debug("실시간 차트 위젯 정리 완료.")
        except Exception as e:
            self.logger.error(f"차트 위젯 정리 중 오류: {e}")

        # 2. 비동기 리소스 정리 (API 연결 해제 등)
        shutdown_task = asyncio.ensure_future(self._shutdown_async())
        # 3. 비동기 작업 완료 후 애플리케이션 종료
        shutdown_task.add_done_callback(self._on_shutdown_complete)
        
        event.accept()

    def _on_shutdown_complete(self, future):
        """비동기 종료 작업 완료 후 호출되는 콜백"""
        self.logger.info("✅ 모든 비동기 리소스 정리 완료. 애플리케이션을 종료합니다.")
        app = QApplication.instance()
        if app:
            app.quit()

    def _shutdown_sync(self):
        """동기 종료 처리 (Fallback)"""
        if self.login_handler:
            self.login_handler.save_settings_sync()
        sys.exit(0)

    async def _shutdown_async(self):
        """비동기 종료 처리"""
        # 0. 진행 중인 모든 차트 데이터 수집 작업 취소
        if self.chart_cache and hasattr(self.chart_cache, 'active_chart_tasks'):
            active_tasks = list(self.chart_cache.active_chart_tasks.values())
            if active_tasks:
                self.logger.debug(f"🔌 진행 중인 {len(active_tasks)}개의 차트 데이터 수집 작업을 취소합니다.")
                for task in active_tasks:
                    task.cancel()
                try:
                    await asyncio.gather(*active_tasks, return_exceptions=True)
                except asyncio.CancelledError:
                    pass # 예상된 예외
        # 1. 웹소켓 연결 해제
        if self.login_handler and hasattr(self.login_handler, 'websocket_client') and self.login_handler.websocket_client:
            self.logger.info("🔌 웹소켓 연결 해제를 시도합니다...")
            await self.login_handler.websocket_client.disconnect()

        # 2. REST 클라이언트 연결 해제
        if self.login_handler and self.login_handler.kiwoom_client:
            await self.login_handler.kiwoom_client.disconnect()

        # 3. 설정 저장
        if self.login_handler:
            await self.login_handler.save_settings()

        # 4. 모든 타이머 중지
        if self.autotrader: self.autotrader.stop_auto_trading()
        if self.chart_cache: self.chart_cache.stop()
        if hasattr(self, 'trading_tab') and hasattr(self.trading_tab, 'realtime_chart_widget'):
            if self.trading_tab.realtime_chart_widget:
                self.trading_tab.realtime_chart_widget.chart_render_timer.stop()

        # 5. 남아있는 비동기 태스크 취소
        try:
            current_task = asyncio.current_task()
            tasks = [t for t in asyncio.all_tasks() if t is not current_task]
            if tasks:
                self.logger.debug(f"🔌 남아있는 {len(tasks)}개의 비동기 태스크를 취소합니다.")
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as ex:
            self.logger.warning(f"비동기 태스크 취소 중 오류 발생: {ex}")

        self.logger.debug("비동기 리소스 정리 완료.")

    async def attempt_auto_connect(self):
        """자동 연결 시도"""
        try:
            # 자동 연결이 활성화되어 있고, 아직 post_login_setup이 실행되지 않았다면
            if self.login_handler.config.getboolean('LOGIN', 'autoconnect', fallback=False):
                if self._post_login_setup_done:
                    logging.debug("이미 자동 연결 및 초기화가 완료되었습니다.")
                    return
                await self.login_handler.handle_api_connection()
                await self.login_handler.start_websocket_client()
                
        except Exception as ex:
            self.logger.error(f"자동 연결 시도 실패: {ex}")    

    def subscribe_holdings_realtime(self, holding_codes):
        """보유종목에 대한 실시간 구독 실행 (중단됨)"""
        try:
            # 실시간 구독 요청 중단
            logging.debug(f"⏸️ 보유종목 실시간 구독 중단: {holding_codes}")
        except Exception as ex:
            self.logger.error(f"❌ 보유종목 실시간 구독 실패: {ex}")
            self.logger.error(f"보유종목 구독 예외 상세: {traceback.format_exc()}")
    
    def extract_monitoring_stock_codes(self):
        """모니터링 종목 코드 추출 (MonitoringManager로 위임)"""
        return self.monitoring_manager.extract_monitoring_stock_codes_enhanced()

    async def post_login_setup(self):
        """로그인 후 설정"""
        try:
            # 중복 실행 방지

            # 3. 시그널 연결
            try:
                if self.trader:
                    self.logger.debug("🔍 트레이더 시그널 연결 중...")
                    self.trader.signal_update_balance.connect(self.update_acnt_balance_display) # type: ignore
                    self.trader.signal_order_result.connect(self.update_order_result) # type: ignore
                    self.logger.debug("✅ 트레이더 시그널 연결 완료")
                else:
                    self.logger.warning("⚠️ 트레이더 객체가 없어 시그널 연결을 건너뜁니다")
                if self.objstg: # type: ignore
                    self.logger.debug("🔍 전략 시그널 연결 중...")
                    self.objstg.signal_strategy_result.connect(self.update_strategy_result)
                    self.logger.debug("✅ 전략 시그널 연결 완료")
                else:
                    self.logger.warning("⚠️ 전략 객체가 없어 시그널 연결을 건너뜁니다")
            except Exception as signal_ex:
                self.logger.error(f"❌ 시그널 연결 실패: {signal_ex}", exc_info=True)

            # 4. 차트 데이터 캐시 초기화
            try:
                if not self.chart_cache:
                    self.chart_cache = ChartDataCache(self.trader, self)
                    self.logger.debug("🔍 ChartDataCache 객체 생성 완료")
                    
                    # 실시간 차트 위젯과 데이터 캐시 연결 (TradingTabWidget 내부)
                    if hasattr(self.trading_tab, 'realtime_chart_widget') and self.trading_tab.realtime_chart_widget:
                        self.chart_cache.data_updated.connect(self.on_chart_data_updated) # type: ignore
                        self.logger.debug("🔍 실시간 차트 위젯과 데이터 캐시 연결 완료")
                if hasattr(self.login_handler, 'kiwoom_client') and self.login_handler.kiwoom_client:
                    self.login_handler.kiwoom_client.chart_cache = self.chart_cache
                    self.logger.debug("🔍 chart_cache를 KiwoomRestClient에 설정 완료")
                self.logger.debug("✅ 차트 데이터 캐시 초기화 완료")
            except Exception as cache_ex:
                self.logger.error(f"❌ 차트 데이터 캐시 초기화 실패: {cache_ex}", exc_info=True)
                self.chart_cache = None

            # 5. 자동매매 객체 초기화 및 시작
            if not self.autotrader: # type: ignore
                self.autotrader = AutoTrader(self.trader, self)
                self.logger.debug("🔍 AutoTrader 객체 생성 완료")
                
                # AutoTrader 자동 시작 (evaluation_interval 주기로 매매 판단)
                self.autotrader.start_auto_trading()
                self.logger.debug(f"✅ 자동매매 시작 완료 ({self.trader.evaluation_interval}초 주기)")

            # 6. 조건검색 목록조회 (웹소켓)
            try:
                # 웹소켓 클라이언트가 연결되어 있는지 확인
                if hasattr(self.login_handler, 'websocket_client') and self.login_handler.websocket_client:
                    if self.login_handler.websocket_client.connected: # 조건검색 목록조회
                        # 웹소켓을 통한 조건검색 목록조회
                        await self.handle_condition_search_list_query()
                        self.logger.debug("✅ 조건검색 목록조회 완료 (웹소켓)")
                    else:
                        self.logger.warning("⚠️ 웹소켓이 연결되지 않아 조건검색 목록조회를 건너뜁니다")
                        self.logger.debug(f"🔍 웹소켓 연결 상태: connected={self.login_handler.websocket_client.connected}")
                else:
                    self.logger.warning("⚠️ 웹소켓 클라이언트가 없어 조건검색 목록조회를 건너뜁니다")
                    self.logger.debug(f"🔍 login_handler.websocket_client 존재: {hasattr(self.login_handler, 'websocket_client')}")
                    if hasattr(self.login_handler, 'websocket_client'):
                        self.logger.debug(f"🔍 websocket_client 값: {self.login_handler.websocket_client}")
            except Exception as condition_ex:
                self.logger.error(f"❌ 조건검색 목록조회 실패: {condition_ex}", exc_info=True)

            # 7. 전체 종목 코드 캐싱
            try:
                await self.data_manager._cache_all_stock_codes_async()
            except Exception as cache_ex:
                self.logger.error(f"❌ 전체 종목 코드 캐싱 실패: {cache_ex}", exc_info=True)

            # 7. 계좌 잔고조회 (즉시 실행)
            try:
                await self.account_manager.handle_acnt_balance_query_async()
                self.logger.debug("✅ 계좌 잔고조회 즉시 실행 완료 (비동기)")
            except Exception as balance_ex:
                self.logger.error(f"❌ 계좌 잔고조회 실행 실패: {balance_ex}", exc_info=True)
            
            # 8. 백테스팅 탭의 DB 기간 로드
            self.backtest_manager.load_db_period()
            self.logger.debug("✅ 백테스팅 탭 DB 기간 로드 완료")

            # 8. 대기 중인 API 큐 처리 (트레이더 객체 생성 후)
            try:
                if hasattr(self, 'chart_cache') and self.chart_cache:
                    if hasattr(self.chart_cache, 'api_request_queue'):
                        queue_size = len(self.chart_cache.api_request_queue)
                        if queue_size > 0:
                            self.logger.debug(f"🔧 대기 중인 API 큐 처리 시작: {queue_size}개 종목")
                            # 큐 처리 타이머 시작 (3초 간격으로 자동 처리)
                            self.chart_cache._start_queue_processing()
                            self.logger.debug("✅ 대기 중인 API 큐 처리 타이머 시작")
                        else:
                            self.logger.debug("🔍 대기 중인 API 큐가 없습니다")
                    else:
                        self.logger.debug("🔍 API 큐가 초기화되지 않았습니다")
                else:
                    self.logger.debug("🔍 차트 캐시가 초기화되지 않았습니다")
            except Exception as queue_ex:
                self.logger.error(f"❌ API 큐 처리 실패: {queue_ex}", exc_info=True)

        except Exception as ex:
            self.logger.error(f"❌ 로그인 후 초기화 실패: {ex}", exc_info=True)
            self.logger.debug("⚠️ 초기화 실패했지만 프로그램을 계속 실행합니다")
        finally:
            # 실행 완료 플래그 설정
            self._post_login_setup_done = True
    
    # --- UI 업데이트 및 이벤트 핸들러 (각 Manager에 위임) ---

    def update_connection_ui(self, is_connected: bool):
        """연결 상태에 따라 UI를 업데이트하는 중앙 함수"""
        if not hasattr(self, 'trading_tab'): return
        
        if is_connected:
            self.trading_tab.connectionStatusLabel.setText("연결 상태: 연결됨")
            self.trading_tab.connectionStatusLabel.setProperty("class", "connected")
            self.trading_tab.connectButton.setText("해제")
            self.trading_tab.connectButton.setProperty("class", "danger")
            self.trading_tab.tradingModeCombo.setEnabled(False)
        else:
            self.trading_tab.connectionStatusLabel.setText("연결 상태: 미연결")
            self.trading_tab.connectionStatusLabel.setProperty("class", "disconnected")
            self.trading_tab.connectButton.setText("연결")
            self.trading_tab.connectButton.setProperty("class", "success")
            self.trading_tab.tradingModeCombo.setEnabled(True)

        self.style().polish(self.trading_tab.connectionStatusLabel)
        self.style().polish(self.trading_tab.connectButton)
        self.style().polish(self.trading_tab.tradingModeCombo)

    def update_acnt_balance_display(self, balance_data: dict):
        """잔고 정보 UI 업데이트"""
        # 이 메서드는 현재 사용되지 않음. update_stock_table로 대체됨.
        self.update_stock_table()

    def update_stock_table(self):
        """투자 현황표 UI 업데이트"""
        if hasattr(self, 'trading_tab'):
            self.trading_tab.stock_table.setRowCount(0)
            if self.login_handler and self.login_handler.websocket_client:
                balance_data = self.login_handler.websocket_client.balance_data
                for row, (code, info) in enumerate(balance_data.items()):
                    self.trading_tab.stock_table.insertRow(row)
                    self.trading_tab.stock_table.setItem(row, 0, QTableWidgetItem(code))
                    self.trading_tab.stock_table.setItem(row, 1, QTableWidgetItem(f"{info.get('current_price', 0):,.0f}"))
                    self.trading_tab.stock_table.setItem(row, 2, QTableWidgetItem(f"{info.get('quantity', 0):,}"))
                    self.trading_tab.stock_table.setItem(row, 3, QTableWidgetItem(f"{info.get('average_price', 0):,.0f}"))
                    profit_loss = info.get('profit_loss', 0)
                    pl_item = QTableWidgetItem(f"{profit_loss:,.0f}")
                    pl_item.setForeground(QColor('green') if profit_loss > 0 else QColor('red') if profit_loss < 0 else QColor('black'))
                    self.trading_tab.stock_table.setItem(row, 4, pl_item)
                    rate_item = QTableWidgetItem(f"{info.get('profit_loss_rate', 0):.2f}%")
                    rate_item.setForeground(QColor('green') if profit_loss > 0 else QColor('red') if profit_loss < 0 else QColor('black'))
                    self.trading_tab.stock_table.setItem(row, 5, rate_item)

    def update_order_result(self, code: str, order_type: str, quantity: int, price: float, success: bool):
        """주문 결과 UI 업데이트"""
        # 로그는 이미 KiwoomTrader에서 출력되므로 UI 업데이트만 수행
        self.update_stock_table()

    def update_strategy_result(self, code: str, action: str, data: dict):
        """전략 결과 UI 업데이트"""
        # 로그는 이미 KiwoomStrategy에서 출력됨
        pass

    def on_chart_data_updated(self, code: str):
        """차트 데이터 업데이트 시그널 핸들러"""
        if hasattr(self, 'trading_tab') and self.trading_tab.realtime_chart_widget.current_code == code:
            # 주기적 업데이트가 차트에 반영되도록 optimized_plot_charts를 직접 호출
            cache_data = self.chart_cache.get_cached_data(code)
            if cache_data:
                tic_data = cache_data.get('tic_data')
                min_data = cache_data.get('min_data')
                self.trading_tab.realtime_chart_widget.optimized_plot_charts(tic_data, min_data)

    async def listBoxChanged(self, current: QListWidgetItem):
        """리스트박스 클릭 이벤트 핸들러"""
        if not self.chart_drawing_lock.acquire(blocking=False):
            self.logger.warning("listBoxChanged is already running. Skipping.")
            return
        try:
            if current:
                item_text = current.text()
                code = item_text.split()[0]
                if hasattr(self, 'trading_tab'):
                    self.trading_tab.realtime_chart_widget.set_current_code(code)
        finally:
            self.chart_drawing_lock.release()

    # --- 조건검색 관련 메서드 (ConditionSearchManager 위임) ---

    async def handle_condition_search_list_query(self):
        """조건검색 목록 조회 (ConditionSearchManager 위임)"""
        await self.condition_search_manager.handle_condition_search_list_query()

    async def handle_integrated_condition_search(self):
        """통합 조건검색 실행 (ConditionSearchManager 위임)"""
        if hasattr(self, 'condition_search_manager'):
            await self.condition_search_manager.handle_integrated_condition_search()

    async def start_condition_realtime(self, seq, condition_name=None):
        """조건검색 실시간 요청으로 지속적 모니터링 시작 (웹소켓 기반)"""
        try:
            # 웹소켓 클라이언트 확인
            if not hasattr(self.login_handler, 'websocket_client') or not self.login_handler.websocket_client:
                self.logger.error("❌ 웹소켓 클라이언트가 연결되지 않았습니다")
                return
            
            if not self.login_handler.websocket_client.connected:
                self.logger.error("❌ 웹소켓이 연결되지 않았습니다")
                return
            
            # 현재 조건검색 이름 저장 (응답 처리 시 사용)
            if condition_name:
                self.current_condition_name = condition_name
                self.logger.debug(f"🔍 조건검색 실시간 요청 시작 (웹소켓): {seq} ({condition_name})")
            else:
                self.logger.debug(f"🔍 조건검색 실시간 요청 시작 (웹소켓): {seq}")
            
            # 웹소켓을 통한 조건검색 실시간 요청 (예시코드 방식)
            await self.login_handler.websocket_client.send_message({
                'trnm': 'CNSRREQ',  # 조건검색 실시간 요청 TR명 (예시코드 방식)
                'seq': seq,
                'search_type': '1',  # 조회타입 (실시간)
                'stex_tp': 'K'  # 거래소구분
            })
            
            if condition_name:
                self.logger.debug(f"✅ 조건검색 실시간 요청 전송 완료 (웹소켓): {seq} ({condition_name}). 응답 대기 중...")
            else:
                self.logger.debug(f"✅ 조건검색 실시간 요청 전송 완료 (웹소켓): {seq}. 응답 대기 중...")
                
        except Exception as ex:
            self.logger.error(f"❌ 조건검색 실시간 요청 실패: {ex}", exc_info=True)

    async def execute_condition_search(self, seq, name):
        """ConditionSearchManager로부터 호출되어 실제 조건검색을 실행하는 메서드"""
        try:
            if seq is not None and name:
                await self.start_condition_realtime(seq, name)
        except Exception as ex:
            self.logger.error(f"조건검색 실행 위임 처리 실패: {ex}", exc_info=True)

    async def stop_condition_realtime(self, seq):
        """조건검색 실시간 해제 (웹소켓 기반)"""
        try:
            # 웹소켓 클라이언트 확인
            if not hasattr(self.login_handler, 'websocket_client') or not self.login_handler.websocket_client:
                self.logger.error("❌ 웹소켓 클라이언트가 연결되지 않았습니다")
                return
            
            if not self.login_handler.websocket_client.connected:
                self.logger.error("❌ 웹소켓이 연결되지 않았습니다")
                return
            
            # 웹소켓을 통한 조건검색 실시간 해제
            await self.login_handler.websocket_client.send_message({
                'trnm': 'CNSRCLR',  # 조건검색 실시간 해제 TR명
                'seq': seq
            })
            
            self.logger.debug(f"✅ 조건검색 실시간 해제 전송 완료 (웹소켓): {seq}")
                
        except Exception as ex:
            self.logger.error(f"❌ 조건검색 실시간 해제 실패: {ex}", exc_info=True)


async def main():
    """메인 실행 함수 - qasync를 사용한 비동기 처리"""
    global main_window
    setup_logging()
    logging.debug("🚀 프로그램 시작 - 로깅 설정 완료")

    app = qasync.QApplication(sys.argv)
    if not app:
        app = QApplication.instance()

    window = MyWindow()
    main_window = window  # 전역 변수에 할당

    # UI가 생성된 후 로깅 재설정하여 QTextEditLogger 추가
    setup_logging() # type: ignore
    window.show()
    
    # ChartDataCache의 타이머들을 메인 스레드에서 초기화 및 시작
    if window.chart_cache:
        window.chart_cache.update_timer.timeout.connect(window.chart_cache.update_all_charts)
        window.chart_cache.save_timer.timeout.connect(window.chart_cache._trigger_async_save_to_database) # type: ignore
        window.chart_cache.queue_timer.timeout.connect(window.chart_cache._process_api_queue)
        
        window.chart_cache.save_timer.start(60000)  # 1분마다 DB 저장
        window.chart_cache.queue_timer.start(2000)  # 2초마다 큐 처리 시작
        window.logger.info("✅ ChartDataCache 타이머 시작 완료 (메인 스레드)")
    
    # qasync가 관리하는 이벤트 루프가 종료될 때까지 대기
    loop = asyncio.get_running_loop()
    await loop.create_future()


if __name__ == "__main__":
    # QApplication이 이미 존재하는지 확인
    try:
        qasync.run(main())
    except asyncio.CancelledError:
        logging.info("프로그램이 정상적으로 종료되었습니다.") # type: ignore
    except Exception as e:
        logging.critical(f"메인 실행 중 치명적인 오류 발생: {e}", exc_info=True)
        # 오류 메시지 박스 표시
        app = QApplication.instance()
        # if app:
        #     QMessageBox.critical(None, "치명적 오류", f"프로그램 실행 중 오류가 발생했습니다:\n{e}")
        sys.exit(1)