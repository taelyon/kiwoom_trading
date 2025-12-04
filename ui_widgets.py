import logging
import asyncio
import time
import traceback
from datetime import datetime, time as dt_time
import configparser
import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QPushButton, QCheckBox, QComboBox, QListWidget, 
                             QTextEdit, QGroupBox, QSplitter, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QAbstractItemView, 
                             QDateEdit, QProgressBar, QListWidgetItem, QSizePolicy, QFrame, QLineEdit, QTabWidget)
from PyQt6.QtCore import Qt, QTimer, QDate, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QPen, QFont
from utils import safe_float_conversion
from chart_data import ChartDataCache

class TradingTabWidget(QWidget):
    """실시간 매매 탭 위젯"""
    def __init__(self, parent: 'MyWindow'):
        super().__init__(parent)
        self.parent_window = parent
        self.logger = logging.getLogger(self.__class__.__name__)
        self.init_ui()

    def init_ui(self):
        """실시간 매매 탭 UI 초기화"""
        parent = self.parent_window

        # ===== 키움 REST API 연결 영역 =====
        loginLayout = QVBoxLayout()

        # API 연결 상태 표시
        statusLayout = QHBoxLayout()
        self.connectionStatusLabel = QLabel("연결 상태: 미연결")
        self.connectionStatusLabel.setProperty("class", "disconnected")
        statusLayout.addWidget(self.connectionStatusLabel)

        statusLayout.addStretch()
        # 자동 연결 설정 (연결 상태 옆으로 이동)
        self.autoConnectCheckBox = QCheckBox("자동 연결")
        statusLayout.addWidget(self.autoConnectCheckBox)
        
        # 모의투자/실제투자 구분
        tradingModeLayout = QHBoxLayout()
        
        self.tradingModeCombo = QComboBox()
        self.tradingModeCombo.addItem("모의투자")
        self.tradingModeCombo.addItem("실제투자")
        self.tradingModeCombo.setFixedWidth(120)
        tradingModeLayout.addWidget(self.tradingModeCombo)
        
        tradingModeLayout.addStretch()
        # 연결/해제 토글 버튼 추가
        self.connectButton = QPushButton("연결")
        self.connectButton.setFixedWidth(80)
        self.connectButton.setProperty("class", "success") # 초기 상태는 '연결' (성공 클래스)
        tradingModeLayout.addWidget(self.connectButton)

        loginLayout.addLayout(statusLayout)
        loginLayout.addLayout(tradingModeLayout)
        
        # 구분선 추가
        separator1 = QFrame()
        separator1.setProperty("class", "separator")
        loginLayout.addWidget(separator1)

        # ===== 투자 설정 =====
        buycountLayout = QHBoxLayout()
        buycountLabel = QLabel("최대투자 종목수:")
        buycountLayout.addWidget(buycountLabel)
        
        # settings.ini에서 저장된 값 읽어오기
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            saved_buycount = config.getint('BUYCOUNT', 'target_buy_count') if config.has_option('BUYCOUNT', 'target_buy_count') else 3
        except:
            saved_buycount = 3
        
        self.buycountEdit = QLineEdit(str(saved_buycount))
        buycountLayout.addWidget(self.buycountEdit)
        buycountLayout.addStretch()
        self.buycountButton = QPushButton("설정")
        self.buycountButton.setFixedWidth(70)
        buycountLayout.addWidget(self.buycountButton)

        # ===== 모니터링 종목 리스트 =====
        monitoringBoxLayout = QVBoxLayout()
        listBoxLabel = QLabel("모니터링 종목:")
        monitoringBoxLayout.addWidget(listBoxLabel)
        
        # 종목 입력 영역
        inputLayout = QHBoxLayout()
        self.stockInputEdit = QLineEdit()
        self.stockInputEdit.setPlaceholderText("종목명 또는 종목코드 입력 (예: 삼성전자, 005930)")
        inputLayout.addWidget(self.stockInputEdit)
        self.addStockButton = QPushButton("추가")
        self.addStockButton.setFixedWidth(60)
        inputLayout.addWidget(self.addStockButton)
        monitoringBoxLayout.addLayout(inputLayout)
        
        self.monitoringBox = QListWidget()
        self.monitoringBox.setEnabled(False)
        monitoringBoxLayout.addWidget(self.monitoringBox, 1)
        
        # 모니터링 종목은 조건검색으로만 추가됨
        firstButtonLayout = QHBoxLayout()
        self.buyButton = QPushButton("매입")
        self.buyButton.setProperty("class", "success")
        firstButtonLayout.addWidget(self.buyButton)
        self.deleteFirstButton = QPushButton("삭제")        
        self.deleteFirstButton.setProperty("class", "danger")
        firstButtonLayout.addWidget(self.deleteFirstButton)        
        monitoringBoxLayout.addLayout(firstButtonLayout)

        # ===== 보유 종목 리스트 =====
        boughtBoxLayout = QVBoxLayout()
        boughtBoxLabel = QLabel("보유 종목:")
        boughtBoxLayout.addWidget(boughtBoxLabel)
        self.boughtBox = QListWidget()
        self.boughtBox.setEnabled(False)
        boughtBoxLayout.addWidget(self.boughtBox, 1)
        secondButtonLayout = QHBoxLayout()
        self.sellButton = QPushButton("매도")
        self.sellButton.setProperty("class", "danger")
        secondButtonLayout.addWidget(self.sellButton)
        self.sellAllButton = QPushButton("전부 매도")
        self.sellAllButton.setProperty("class", "danger")
        secondButtonLayout.addWidget(self.sellAllButton)     
        boughtBoxLayout.addLayout(secondButtonLayout)

        # ===== 왼쪽 영역 통합 =====
        listBoxesLayout = QVBoxLayout()
        listBoxesLayout.addLayout(loginLayout)
        listBoxesLayout.addLayout(buycountLayout)
        listBoxesLayout.addLayout(monitoringBoxLayout, 6)
        listBoxesLayout.addLayout(boughtBoxLayout, 4)

        # ===== 실시간 차트 영역 =====
        chartLayout = QVBoxLayout()

        # PyQtGraph 기반 차트 위젯 사용
        self.realtime_chart_widget = PyQtGraphRealtimeWidget(parent)
        
        # 실시간 차트 위젯을 레이아웃에 추가        
        if not parent.chart_cache:
            parent.chart_cache = ChartDataCache(None, parent)  # 트레이더는 API 연결 후 설정됨
        
        # ===== 차트와 리스트 통합 =====
        chartLayout.addWidget(self.realtime_chart_widget)

        chartAndListLayout = QHBoxLayout()
        chartAndListLayout.addLayout(listBoxesLayout, 1)
        chartAndListLayout.addLayout(chartLayout, 4)

        # ===== 전략 및 거래 정보 영역 =====
        strategyAndTradeLayout = QVBoxLayout()

        # 투자 전략
        strategyLayout = QHBoxLayout()
        strategyLabel = QLabel("투자전략:")
        strategyLabel.setFixedWidth(70)
        strategyLayout.addWidget(strategyLabel, alignment=Qt.AlignmentFlag.AlignLeft)
        self.comboStg = QComboBox()
        self.comboStg.setFixedWidth(200)
        strategyLayout.addWidget(self.comboStg, alignment=Qt.AlignmentFlag.AlignLeft)
        strategyLayout.addStretch()

        # 매수 전략
        buyStrategyLayout = QHBoxLayout()
        buyStgLabel = QLabel("매수전략:")
        buyStgLabel.setFixedWidth(70)
        buyStrategyLayout.addWidget(buyStgLabel, alignment=Qt.AlignmentFlag.AlignLeft)
        self.comboBuyStg = QComboBox()
        self.comboBuyStg.setFixedWidth(200)
        buyStrategyLayout.addWidget(self.comboBuyStg, alignment=Qt.AlignmentFlag.AlignLeft)
        buyStrategyLayout.addStretch()
        self.saveBuyStgButton = QPushButton("수정")
        self.saveBuyStgButton.setFixedWidth(100)
        buyStrategyLayout.addWidget(self.saveBuyStgButton, alignment=Qt.AlignmentFlag.AlignRight)
        self.buystgInputWidget = QTextEdit()
        self.buystgInputWidget.setPlaceholderText("매수전략의 내용을 입력하세요...")
        self.buystgInputWidget.setFixedHeight(80)

        # 매도 전략
        sellStrategyLayout = QHBoxLayout()
        sellStgLabel = QLabel("매도전략:")
        sellStgLabel.setFixedWidth(70)
        sellStrategyLayout.addWidget(sellStgLabel, alignment=Qt.AlignmentFlag.AlignLeft)
        self.comboSellStg = QComboBox()
        self.comboSellStg.setFixedWidth(200)
        sellStrategyLayout.addWidget(self.comboSellStg, alignment=Qt.AlignmentFlag.AlignLeft)
        sellStrategyLayout.addStretch()
        self.saveSellStgButton = QPushButton("수정")
        self.saveSellStgButton.setFixedWidth(100)
        sellStrategyLayout.addWidget(self.saveSellStgButton, alignment=Qt.AlignmentFlag.AlignRight)
        self.sellstgInputWidget = QTextEdit()
        self.sellstgInputWidget.setPlaceholderText("매도전략의 내용을 입력하세요...")
        self.sellstgInputWidget.setFixedHeight(63)

        # 비동기 슬롯을 안전하게 생성하기 위한 헬퍼 함수
        def _safe_create_task(coro):
            try:
                asyncio.create_task(coro)
            except RuntimeError:
                self.logger.warning("⚠️ 이벤트 루프가 없어 비동기 작업을 실행할 수 없습니다")

        # 주식 현황 테이블
        self.stock_table = QTableWidget()
        self.stock_table.setRowCount(0)
        self.stock_table.setColumnCount(6)
        self.stock_table.setHorizontalHeaderLabels(["종목코드", "현재가", "보유수량", "매입단가", "평가손익", "수익률"])
        self.stock_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.stock_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.stock_table.setFixedHeight(220)
        self.stock_table.verticalHeader().setDefaultSectionSize(20)

        strategyAndTradeLayout.addLayout(strategyLayout)
        strategyAndTradeLayout.addLayout(buyStrategyLayout)
        strategyAndTradeLayout.addWidget(self.buystgInputWidget)
        strategyAndTradeLayout.addLayout(sellStrategyLayout)
        strategyAndTradeLayout.addWidget(self.sellstgInputWidget)
        strategyAndTradeLayout.addWidget(self.stock_table)

        # ===== 터미널 출력 =====
        self.terminalOutput = QTextEdit()
        self.terminalOutput.setReadOnly(True)
        self.terminalOutput.setProperty("class", "terminal")

        counterAndterminalLayout = QVBoxLayout()
        counterAndterminalLayout.addLayout(strategyAndTradeLayout)
        counterAndterminalLayout.addWidget(self.terminalOutput)

        # ===== 메인 레이아웃 =====
        mainLayout = QHBoxLayout()
        mainLayout.addLayout(chartAndListLayout, 70)
        mainLayout.addLayout(counterAndterminalLayout, 30)
        self.setLayout(mainLayout)

        # ===== 이벤트 연결 =====
        self.tradingModeCombo.currentIndexChanged.connect(parent.trading_manager.trading_mode_changed)
        self.buycountButton.clicked.connect(parent.trading_manager.buycount_setting)
        self.addStockButton.clicked.connect(parent.trading_manager.add_stock_to_list)

        # 비동기 슬롯 연결
        self.connectButton.clicked.connect(lambda: _safe_create_task(parent.login_handler._handle_connection_toggle_async()))
        self.stockInputEdit.returnPressed.connect(lambda: _safe_create_task(parent.trading_manager.add_stock_to_list()))
        self.buyButton.clicked.connect(lambda: _safe_create_task(parent.trading_manager.buy_item()))
        self.deleteFirstButton.clicked.connect(lambda: _safe_create_task(parent.trading_manager.delete_select_item()))
        self.sellButton.clicked.connect(lambda: _safe_create_task(parent.trading_manager.sell_item()))
        self.sellAllButton.clicked.connect(lambda: _safe_create_task(parent.trading_manager.sell_all_item()))

        # 리스트박스 이벤트 연결
        self.monitoringBox.itemClicked.connect(lambda item: _safe_create_task(parent.listBoxChanged(item)))
        self.boughtBox.itemClicked.connect(lambda item: _safe_create_task(parent.listBoxChanged(item)))
        self.logger.debug("✅ 리스트박스 클릭 이벤트 연결 완료")
        
        # 리스트박스 활성화
        self.monitoringBox.setEnabled(True)
        self.boughtBox.setEnabled(True)
        self.logger.debug("✅ 리스트박스 활성화 완료")
        
        # 전략 변경 핸들러를 비동기로 연결
        self.comboStg.currentIndexChanged.connect(lambda: _safe_create_task(parent.strategy_manager.stg_changed()))
        self.comboBuyStg.currentIndexChanged.connect(parent.strategy_manager.buy_stg_changed)
        self.comboSellStg.currentIndexChanged.connect(parent.strategy_manager.sell_stg_changed)
        self.saveBuyStgButton.clicked.connect(parent.strategy_manager.save_buystrategy)
        self.saveSellStgButton.clicked.connect(parent.strategy_manager.save_sellstrategy)


class BacktestTabWidget(QWidget):
    """백테스팅 탭 위젯"""
    def __init__(self, parent: 'MyWindow'):
        super().__init__(parent)
        self.parent_window = parent
        self.logger = logging.getLogger(self.__class__.__name__)
        self.init_ui()

    def init_ui(self):
        """백테스팅 탭 UI 초기화"""
        parent = self.parent_window
        layout = QVBoxLayout()
        
        # ===== 설정 영역 =====
        settings_group = QGroupBox("백테스팅 설정")
        settings_layout = QHBoxLayout()
        
        # 좌측 그룹: 기간 설정
        period_group = QWidget()
        period_layout = QHBoxLayout()
        period_layout.setContentsMargins(0, 0, 0, 0)
        
        # 시작일 (기본값: 1개월 전)
        period_layout.addWidget(QLabel("시작일:"))
        self.bt_start_date = QLineEdit()
        self.bt_start_date.setPlaceholderText("YYYYMMDD")
        self.bt_start_date.setFixedWidth(120)
        
        # 종료일 (기본값: 오늘)
        period_layout.addWidget(QLabel("종료일:"))
        self.bt_end_date = QLineEdit()
        self.bt_end_date.setPlaceholderText("YYYYMMDD")
        self.bt_end_date.setFixedWidth(120)

        # 기본값 설정 (1개월)
        today = QDate.currentDate()
        one_month_ago = today.addMonths(-1)
        self.bt_start_date.setText(one_month_ago.toString("yyyyMMdd"))
        self.bt_end_date.setText(today.toString("yyyyMMdd"))
        
        period_layout.addWidget(self.bt_start_date)
        period_layout.addWidget(self.bt_end_date)
        
        # DB 기간 불러오기 버튼
        self.bt_load_period_button = QPushButton("DB 기간 불러오기")
        self.bt_load_period_button.setFixedWidth(150)
        self.bt_load_period_button.clicked.connect(parent.backtest_manager.load_db_period)
        period_layout.addWidget(self.bt_load_period_button)
        
        period_group.setLayout(period_layout)
        settings_layout.addWidget(period_group)
        
        # 중간 스트레치
        settings_layout.addStretch(1)
        
        # 초기 자금
        settings_layout.addWidget(QLabel("초기 자금:"))
        self.bt_initial_cash = QLineEdit("10000000")
        self.bt_initial_cash.setFixedWidth(120)
        settings_layout.addWidget(self.bt_initial_cash)
        settings_layout.addStretch(1)
        
        # 대상 종목 선택
        settings_layout.addWidget(QLabel("대상 종목:"))
        self.bt_stock_combo = QComboBox()
        self.bt_stock_combo.setFixedWidth(120)
        self.bt_stock_combo.addItem("전체 종목")
        settings_layout.addWidget(self.bt_stock_combo)
        settings_layout.addStretch(1)
        
        # 전략 선택
        settings_layout.addWidget(QLabel("투자 전략:"))
        self.bt_strategy_combo = QComboBox()
        self.bt_strategy_combo.setFixedWidth(120)
        settings_layout.addWidget(self.bt_strategy_combo)
        settings_layout.addStretch(1)
        
        # 실행 버튼
        self.bt_run_button = QPushButton("백테스팅 실행")
        self.bt_run_button.setFixedWidth(120)
        self.bt_run_button.clicked.connect(parent.backtest_manager.run_backtest)
        settings_layout.addWidget(self.bt_run_button)
        
        settings_group.setLayout(settings_layout)
        layout.addWidget(settings_group)
        
        # ===== 결과 영역 (탭 구조) =====
        results_tab_widget = QTabWidget()
        
        # 탭 1: 전체 결과
        overall_tab = QWidget()
        overall_layout = QHBoxLayout()
        
        # 왼쪽: 결과 요약
        left_widget = QWidget()
        left_layout = QVBoxLayout()
        
        left_layout.addWidget(QLabel("백테스팅 결과:"))
        self.bt_results_text = QTextEdit()
        self.bt_results_text.setReadOnly(True)
        self.bt_results_text.setMaximumWidth(450)
        left_layout.addWidget(self.bt_results_text)
        
        left_widget.setLayout(left_layout)
        
        # 오른쪽: 차트
        right_widget = QWidget()
        right_layout = QVBoxLayout()
        
        right_widget.setLayout(right_layout)
        
        # 차트 표시용 위젯 추가 (PyQtGraph)
        self.bt_chart_widget = pg.PlotWidget()
        self.bt_chart_widget.setBackground('w')
        self.bt_chart_widget.setTitle("백테스팅 결과 차트")
        self.bt_chart_widget.showGrid(x=True, y=True)
        self.bt_chart_widget.addLegend()
        right_layout.addWidget(self.bt_chart_widget)
        
        overall_layout.addWidget(left_widget, 1)
        overall_layout.addWidget(right_widget, 2)
        overall_tab.setLayout(overall_layout)
        
        # 탭 2: 일별 성과
        daily_tab = QWidget()
        daily_layout = QHBoxLayout()
        
        # 왼쪽: 일별 성과 테이블
        daily_left_widget = QWidget()
        daily_left_layout = QVBoxLayout()
        
        daily_left_layout.addWidget(QLabel("일별 성과 내역:"))
        self.bt_daily_table = QTableWidget()
        self.bt_daily_table.setColumnCount(8)
        self.bt_daily_table.setHorizontalHeaderLabels([
            "날짜", "일손익", "수익률(%)", "거래수", "승", "패", "누적손익", "포트폴리오"
        ])
        self.bt_daily_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.bt_daily_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.bt_daily_table.setMaximumWidth(600)
        daily_left_layout.addWidget(self.bt_daily_table)
        
        daily_left_widget.setLayout(daily_left_layout)
        
        # 오른쪽: 일별 차트
        daily_right_widget = QWidget()
        daily_right_layout = QVBoxLayout()
        
        daily_right_widget.setLayout(daily_right_layout)
        
        daily_layout.addWidget(daily_left_widget, 1)
        daily_layout.addWidget(daily_right_widget, 2)
        daily_tab.setLayout(daily_layout)
        
        # 탭 추가
        results_tab_widget.addTab(overall_tab, "전체 성과")
        results_tab_widget.addTab(daily_tab, "일별 성과")
        
        layout.addWidget(results_tab_widget)
        
        self.setLayout(layout)


# ==================== PyQtGraph CandlesticItem 클래스 ====================
class CandlesticItem(pg.GraphicsObject):
    """PyQtGraph용 캔들스틱 아이템"""
    def __init__(self, data):
        self.logger = logging.getLogger(self.__class__.__name__)
        """
        data: (N, 5) numpy array (timestamp, open, high, low, close)
        """
        pg.GraphicsObject.__init__(self)
        self.data = data  # (timestamp, open, high, low, close)
        self.picture = None
        self.generatePicture()

    def generatePicture(self):
        self.picture = pg.QtGui.QPicture()
        p = pg.QtGui.QPainter(self.picture)
        
        # 데이터가 1개 이상일 때만 폭(w) 계산
        w = 0.0
        if len(self.data) > 1:
            # 타임스탬프 간의 평균 간격을 캔들 폭으로 사용 (일반적)
            # 여기서는 DateAxisItem이 아닌 경우를 대비해 인덱스 기반으로도 계산
            if self.data[-1, 0] > (len(self.data) - 1): # 타임스탬프 기반
                w = (self.data[-1, 0] - self.data[0, 0]) / (len(self.data) - 1) * 0.4
            else: # 인덱스 기반
                 w = 0.4 # 인덱스 1.0 간격의 40%
        else:
            w = 0.4 # 데이터가 하나면 기본 폭

        if w == 0.0: # 데이터가 1개이거나 간격이 0일 때의 예외 처리
            w = 0.4
            
        for (t, open, high, low, close) in self.data:
            # 수직선 (High-Low)
            p.setPen(pg.mkPen('k')) # 'k' = black
            p.drawLine(pg.QtCore.QPointF(t, low), pg.QtCore.QPointF(t, high))

            # 캔들 몸통 (Open-Close)
            if open > close:
                p.setBrush(pg.mkBrush('b')) # 'b' = blue (하락)
                p.setPen(pg.mkPen('b'))
            else:
                p.setBrush(pg.mkBrush('r')) # 'r' = red (상승)
                p.setPen(pg.mkPen('r'))
            
            p.drawRect(pg.QtCore.QRectF(t - w, open, w * 2, close - open))
        
        p.end()

    def setData(self, data):
        self.data = data
        self.generatePicture()
        self.update() # QGraphicsObject.update() 호출

    def paint(self, p, *args):
        if self.picture:
            self.picture.play(p)

    def boundingRect(self):
        if not self.picture:
            return pg.QtCore.QRectF()
        return pg.QtCore.QRectF(self.picture.boundingRect())


# ==================== PyQtGraph 차트 위젯 클래스 ====================
class PyQtGraphWidget(pg.PlotWidget):
    """PyQtGraph 기반 차트 위젯"""
    def __init__(self, parent=None, title="실시간 차트"):
        self.logger = logging.getLogger(self.__class__.__name__)
        super().__init__(parent)
        
        # 차트 설정
        self.setBackground('#f5f5f5')
        self.setTitle(f'<span style="color: black;">{title}</span>')
        self.showGrid(x=True, y=False, alpha=0.5)
        
        # 축 색상 설정
        self.getAxis('bottom').setPen('k')
        self.getAxis('left').setPen('k')
        self.getAxis('bottom').setTextPen('k')
        self.getAxis('left').setTextPen('k')
        
        # 캔들스틱 아이템
        self.candle_item = None
        
        # 선 차트 아이템들
        self.line_items = {}
        
        # 이동평균선 아이템들
        self.ma_lines = {}
        
        # 범례 아이템
        self.legend_item = None
        
        # 데이터 저장
        self.current_data = None
        
    def clear_chart(self):
        """차트 초기화"""
        # 캔들스틱 아이템 제거
        if self.candle_item is not None:
            self.removeItem(self.candle_item)
            self.candle_item = None
        
        # 모든 선 차트 아이템 제거
        for item in list(self.line_items.values()):
            self.removeItem(item)
        self.line_items.clear()
        
        # 모든 이동평균선 제거
        self.clear_moving_averages()
        
        # 범례 제거
        self.clear_legend()
        
        # 데이터 초기화
        self.current_data = None
        
        # PlotItem의 모든 아이템 제거 (확실하게 비우기 - 세로선, 텍스트 등 포함)
        self.plotItem.clear()
        
        # X축 눈금(레이블) 초기화
        if self.getAxis('bottom'):
            self.getAxis('bottom').setTicks([])
        
        # 그리드 설정 복구 (clear()로 인해 초기화될 수 있음)
        self.showGrid(x=True, y=False, alpha=0.5)
        
    def add_candlestic_data(self, data, chart_type="default"):
        """캔들스틱 데이터 추가"""
        try:
            # 데이터 유효성 검사
            if not data or len(data) == 0:
                self.logger.warning("🔍 PyQtGraphWidget add_candlestic_data: 빈 데이터")
                return
                
            self.logger.debug(f"🔍 PyQtGraphWidget add_candlestic_data 호출됨 - 데이터 수: {len(data)}")
            
            # 데이터 형식 검사
            if not isinstance(data, (list, tuple)):
                self.logger.error(f"🔍 PyQtGraphWidget add_candlestic_data: 잘못된 데이터 형식 - {type(data)}")
                return
                
            # 첫 번째 데이터 항목 검사
            if len(data) > 0:
                first_item = data[0]
                if not isinstance(first_item, (list, tuple)) or len(first_item) < 5:
                    self.logger.error(f"🔍 PyQtGraphWidget add_candlestic_data: 잘못된 데이터 구조 - {first_item}")
                    return
                    
            # 기존 캔들 아이템 제거
            if self.candle_item is not None:
                self.removeItem(self.candle_item)
            
            # 데이터 변환 (timestamp, open, high, low, close)
            data_list = []
            for i, item in enumerate(data):
                try:
                    if not isinstance(item, (list, tuple)) or len(item) < 5:
                        self.logger.error(f"🔍 PyQtGraphWidget 잘못된 데이터 항목 {i}: {item}")
                        continue
                        
                    timestamp, open_price, high_price, low_price, close_price = item
                    
                    # 가격 데이터 검사
                    try:
                        open_price = float(open_price)
                        high_price = float(high_price)
                        low_price = float(low_price)
                        close_price = float(close_price)
                    except (ValueError, TypeError) as price_error:
                        self.logger.error(f"🔍 PyQtGraphWidget 가격 데이터 변환 오류 {i}: {price_error}")
                        continue
                    
                    # 인덱스를 타임스탬프로 사용
                    data_list.append((i, open_price, high_price, low_price, close_price))
                    
                    # 첫 번째와 마지막 데이터 디버깅
                    if i == 0:
                        self.logger.debug(f"🔍 PyQtGraphWidget 첫 번째 캔들: O={open_price}, H={high_price}, L={low_price}, C={close_price}")
                    elif i == len(data) - 1:
                        self.logger.debug(f"🔍 PyQtGraphWidget 마지막 캔들: O={open_price}, H={high_price}, L={low_price}, C={close_price}")
                        
                except Exception as item_error:
                    self.logger.error(f"🔍 PyQtGraphWidget 데이터 항목 처리 오류 {i}: {item_error}")
                    continue

            if len(data_list) == 0:
                self.logger.warning("🔍 PyQtGraphWidget 처리 가능한 데이터가 없습니다")
                return
            
            # numpy 배열로 변환
            np_data = np.array(data_list)
            
            # CandlesticItem 생성 및 추가
            self.candle_item = CandlesticItem(np_data)
            self.addItem(self.candle_item)
            
            # 데이터 저장
            self.current_data = data
            
            # 축 범위 설정
            if len(data_list) > 0:
                # X축 범위 설정
                self.setXRange(0, len(data_list) - 1)
                
                # Y축 범위 설정 (가격) - 범례를 위한 공간 확보
                all_prices = []
                for item in data_list:
                    _, open_price, high_price, low_price, close_price = item
                    all_prices.extend([open_price, high_price, low_price, close_price])
                    
                    min_price = min(all_prices)
                    max_price = max(all_prices)
                    price_range = max_price - min_price
                    margin = price_range * 0.1  # 10% 여백
                    
                    # 범례를 위한 추가 공간 확보 (상단에 20% 추가 여백)
                    legend_space = price_range * 0.2  # 범례를 위한 20% 추가 공간
                    top_margin = margin + legend_space  # 상단 여백 증가
                    
                self.logger.debug(f"🔍 PyQtGraphWidget 가격 범위: 최저={min_price:.2f}, 최고={max_price:.2f}, 범위={price_range:.2f}")
                self.setYRange(min_price - margin, max_price + top_margin)
                
                # X축 레이블 수동 설정 (test.py의 setup_index_axis_chart 방식 참고)
                self._setup_x_axis_labels(data, chart_type=chart_type)
                
                self.logger.debug(f"✅ PyQtGraphWidget 캔들 데이터 추가 완료: {len(data_list)}개")
            
        except Exception as ex:
            self.logger.error(f"❌ 캔들스틱 데이터 추가 실패: {ex}")
            self.logger.error(f"❌ 캔들스틱 데이터 추가 오류 상세: {traceback.format_exc()}")
    
    
    def add_line_data(self, data, name="Line", color=None):
        """선 차트 데이터 추가"""
        try:
            self.logger.debug(f"🔍 PyQtGraphWidget add_line_data 호출됨 - 이름: {name}, 데이터 수: {len(data)}")
            
            # 기존 아이템이 있으면 제거
            if name in self.line_items:
                self.removeItem(self.line_items[name])
            
            # 데이터 변환
            x_data = []
            y_data = []
            
            for i, item in enumerate(data):
                if len(item) >= 2:
                    timestamp, price = item[0], item[1]
                    x_data.append(i)
                    y_data.append(float(price))
            
            # 선 차트 아이템 생성
            if color:
                pen = pg.mkPen(color=color)
            else:
                pen = pg.mkPen(color='g')  # 기본 녹색
            
            line_item = pg.PlotDataItem(x_data, y_data, pen=pen, name=name)
            
            # 아이템 추가
            self.addItem(line_item)
            self.line_items[name] = line_item
            
            self.logger.debug(f"✅ PyQtGraphWidget 선 차트 데이터 추가 완료")
            
        except Exception as ex:
            self.logger.error(f"❌ 선 차트 데이터 추가 실패: {ex}")
            self.logger.error(f"❌ 선 차트 데이터 추가 오류 상세: {traceback.format_exc()}")
    
    def add_infinite_line(self, pos, angle=0, pen=None, label=None, labelOpts=None):
        """무한 선 추가 (매입단가 표시용)"""
        try:
            line = pg.InfiniteLine(pos=pos, angle=angle, pen=pen, label=label, labelOpts=labelOpts)
            self.addItem(line)
            return line
        except Exception as ex:
            self.logger.error(f"❌ 무한 선 추가 실패: {ex}")
            return None

    def remove_line_item(self, name):
        """선 차트 아이템 제거"""
        if name in self.line_items:
            self.removeItem(self.line_items[name])
            del self.line_items[name]
    
    def setTitle(self, title):
        """차트 제목 설정"""
        self.plotItem.setTitle(title)
    
    def setXRange(self, min_val, max_val):
        """X축 범위 설정"""
        self.plotItem.setXRange(min_val, max_val)
    
    def setYRange(self, min_val, max_val):
        """Y축 범위 설정"""
        self.plotItem.setYRange(min_val, max_val)
    
    def setMinimumHeight(self, height):
        """최소 높이 설정"""
        super().setMinimumHeight(height)
    
    def setVisible(self, visible):
        """가시성 설정"""
        super().setVisible(visible)
    def size(self):
        """크기 반환"""
        return super().size()
    
    def removeItem(self, item):
        """아이템 제거"""
        self.plotItem.removeItem(item)
    
    def addItem(self, item):
        """아이템 추가"""
        self.plotItem.addItem(item)
    
    def add_moving_averages(self, data, ma_data, chart_type="tic"):
        """이동평균선 추가"""
        try:
            self.logger.debug(f"🔍 add_moving_averages 호출됨 - data: {type(data)}, ma_data: {type(ma_data)}")
            self.logger.debug(f"🔍 ma_data 키: {list(ma_data.keys()) if isinstance(ma_data, dict) else 'Not dict'}")
            
            if not data or not ma_data:
                self.logger.warning(f"⚠️ 데이터가 없습니다 - data: {bool(data)}, ma_data: {bool(ma_data)}")
                return
            
            # 기존 이동평균선 제거
            self.clear_moving_averages()
            
            # 차트 유형별 이동평균선 색상 정의
            if chart_type == "tic":
                # 틱 차트: MA5, MA20, MA60, MA120
                ma_colors = {
                    'MA5': (255, 0, 0),      # 빨간색
                    'MA20': (0, 0, 255),     # 파란색
                    'MA60': (255, 165, 0),   # 주황색
                    'MA120': (128, 0, 128),  # 보라색
                }
            elif chart_type == "minute":
                # 3분봉 차트: MA5, MA10, MA20
                ma_colors = {
                    'MA5': (255, 0, 0),     # 빨간색
                    'MA10': (0, 255, 0),     # 녹색
                    'MA20': (0, 0, 255),     # 파란색
                }
            else:
                # 기본값
                ma_colors = {
                    'MA5': (255, 0, 0),     # 빨간색
                    'MA20': (0, 0, 255),     # 파란색
                }
            
            # 범례 텍스트 생성
            legend_text = f"이동평균선: {', '.join(ma_colors.keys())}"
            self.logger.debug(f"📊 {chart_type} 차트 이동평균선 범례: {legend_text}")
            
            # 각 이동평균선 그리기
            for ma_type, ma_values in ma_data.items():
                # numpy 배열인 경우 길이 확인 방법 수정
                if hasattr(ma_values, '__len__'):
                    ma_length = len(ma_values)
                else:
                    ma_length = 0
                self.logger.debug(f"🔍 {ma_type} 처리 중 - 값 개수: {ma_length}")
                
                if ma_type in ma_colors and ma_values is not None and len(ma_values) > 0:
                    # 유효한 데이터만 필터링
                    valid_data = []
                    for i, value in enumerate(ma_values):
                        if value is not None and not (isinstance(value, float) and (value != value or value == 0)):
                            valid_data.append((i, float(value)))
                    
                    self.logger.debug(f"🔍 {ma_type} 유효한 데이터 개수: {len(valid_data)}")
                    
                    if len(valid_data) > 0:
                        # numpy 배열로 변환
                        ma_array = np.array(valid_data)
                        
                        # 이동평균선 그리기
                        color = ma_colors[ma_type]
                        pen = pg.mkPen(color=color, width=2)
                        
                        ma_line = pg.PlotDataItem(
                            ma_array[:, 0], 
                            ma_array[:, 1], 
                            pen=pen, 
                            name=f"{ma_type}",
                            connect='finite'
                        )
                        
                        self.addItem(ma_line)
                        self.ma_lines[ma_type] = ma_line
                        
                        self.logger.debug(f"✅ {ma_type} 이동평균선 추가: {len(valid_data)}개 데이터")
                    else:
                        self.logger.warning(f"⚠️ {ma_type} 유효한 데이터가 없습니다")
                else:
                    # numpy 배열인 경우 안전한 진리값 확인
                    has_values = ma_values is not None and len(ma_values) > 0
                    self.logger.warning(f"⚠️ {ma_type} 처리 건너뜀 - 색상: {ma_type in ma_colors}, 값: {has_values}, 길이: {len(ma_values) if hasattr(ma_values, '__len__') else 0}")
            
            # 범례 추가
            self.add_legend()
            
        except Exception as ex:
            self.logger.error(f"❌ 이동평균선 추가 실패: {ex}")
            self.logger.error(f"❌ 상세 오류: {traceback.format_exc()}")
    
    def clear_moving_averages(self):
        """이동평균선 제거"""
        try:
            for ma_type, ma_line in self.ma_lines.items():
                if ma_line:
                    self.removeItem(ma_line)
            self.ma_lines.clear()
            
            # 범례도 제거
            self.clear_legend()
            
        except Exception as ex:
            self.logger.error(f"❌ 이동평균선 제거 실패: {ex}")
    
    def add_legend(self):
        """범례 추가"""
        try:
            # 기존 범례 제거
            self.clear_legend()
            
            if not self.ma_lines:
                self.logger.warning("⚠️ 표시할 이동평균선이 없습니다")
                return
            
            # 범례 아이템 생성
            legend_items = []
            for ma_type, ma_line in self.ma_lines.items():
                if ma_line:
                    # 이동평균선의 색상과 이름을 사용하여 범례 아이템 생성
                    pen = ma_line.opts['pen']
                    color = pen.color().name() if hasattr(pen, 'color') else '#FF0000'
                    
                    legend_item = {
                        'name': ma_type,
                        'color': color,
                        'line': ma_line
                    }
                    legend_items.append(legend_item)
            
            if legend_items:
                # PyQtGraph의 LegendItem 사용
                
                # 범례 위치 설정 (차트 내 좌상단, 캔들과 겹치지 않도록)
                # PyQtGraph에서 범례를 좌상단에 배치하기 위해 실제 위젯 크기 사용
                # 차트의 실제 픽셀 크기 가져오기
                chart_size = self.size()
                chart_width = chart_size.width()
                chart_height = chart_size.height()
                
                # 범례 크기와 여백 설정 (줄간격 줄임에 맞게 조정)
                legend_width = 90   # 너비 약간 줄임
                legend_height = 60  # 높이 줄임 (줄간격 감소로 인해)
                margin = 10
                
                # 좌상단 좌표 계산 (차트 좌측 상단에 여백을 둔 위치)
                # Y축 레이블을 가리지 않도록 좌측 여백 확보 (약 70px)
                left_x = 70  # Y축과 레이블을 피하기 위한 충분한 여백
                # 범례를 위한 확보된 공간의 상단에 배치 (차트 높이의 상단 10% 영역)
                top_y = int(chart_height * 0.05)  # 차트 높이의 5% 위치
                
                # 범례 생성 (확보된 공간에 배치)
                self.legend_item = pg.LegendItem(offset=(left_x, top_y), size=(legend_width, legend_height))
                
                self.legend_item.setParentItem(self.plotItem)
                
                # 범례 스타일 설정
                self.legend_item.setBrush('w')  # 흰색 배경
                self.legend_item.setPen('k')    # 검은색 테두리
                self.legend_item.setOpacity(0.9)  # 높은 투명도
                
                # 범례가 다른 요소 위에 표시되도록 설정
                self.legend_item.setZValue(1000)
                
                # 범례 폰트 크기 조정 (PyQt6 호환)
                font = QFont()
                font.setPointSize(7)  # 더 작은 폰트 크기
                font.setBold(True)    # 굵은 글씨
                font.setStyleHint(QFont.StyleHint.SansSerif)  # 명확한 폰트
                # PyQt6에서는 줄간격을 직접 설정할 수 없으므로 폰트 크기로 조정
                self.legend_item.setFont(font)

                # 각 이동평균선을 범례에 추가
                for item in legend_items:
                    self.legend_item.addItem(item['line'], item['name'])
                
                self.logger.debug(f"✅ 범례 추가 완료: {len(legend_items)}개 항목")
            
        except Exception as ex:
            self.logger.error(f"❌ 범례 추가 실패: {ex}")
            self.logger.error(f"❌ 상세 오류: {traceback.format_exc()}")
    
    def clear_legend(self):
        """범례 제거"""
        try:
            if self.legend_item:
                self.legend_item.clear()
                # 씬에서 제거 시도
                if self.legend_item.scene():
                    self.legend_item.scene().removeItem(self.legend_item)
                elif self.plotItem:
                    # PlotItem에서 제거 시도 (setParentItem으로 추가된 경우)
                    try:
                        self.plotItem.removeItem(self.legend_item)
                    except:
                        pass
                
                self.legend_item = None
                self.logger.debug("✅ 범례 제거 완료")
        except Exception as ex:
            self.logger.error(f"❌ 범례 제거 실패: {ex}")
    
    
    
    def showGrid(self, x=True, y=False, alpha=0.5):
        """그리드 표시 - Y축 그리드 제거, X축만 표시"""
        self.plotItem.showGrid(x=x, y=y, alpha=alpha)
        
        # X축 눈금 설정
        if x:
            self._setup_x_axis_tics()
    
    def plotItem(self):
        """플롯 아이템 반환"""
        return self.getPlotItem()
    
    def getAxis(self, axis_name):
        """축 반환"""
        if axis_name == 'bottom':
            return self.getPlotItem().getAxis('bottom')
        elif axis_name == 'left':
            return self.getPlotItem().getAxis('left')
        else:
            return None
    
    def _setup_x_axis_tics(self):
        """X축 눈금 설정"""
        try:
            # X축 설정
            x_axis = self.getAxis('bottom')
            if x_axis:
                # 눈금 표시 설정
                x_axis.setTickSpacing(major=10, minor=5)  # 주요 눈금 10단위, 보조 눈금 5단위
                x_axis.setStyle(showValues=True)  # 값 표시
                x_axis.setGrid(255)  # 그리드 색상 설정
                
        except Exception as ex:
            self.logger.debug(f"X축 눈금 설정 중 오류 (무시됨): {ex}")
    
    def _setup_x_axis_labels(self, data, chart_type="default"):
        """X축 레이블 수동 설정 (test.py의 setup_index_axis_chart 방식 참고)"""
        try:
            if not data or len(data) == 0:
                return
            
            # X축 레이블 수동 설정 (PyQtChart의 QBarCategoryAxis와 동일한 방식)
            axis = self.getAxis('bottom')
            
            tics = []  # (index, "label") 튜플의 리스트
            last_label_minute = -1
            
            # 30분 단위 경계 감지 로직으로 변경 (데이터가 드문 경우에도 정확히 표시하기 위함)
            last_interval_index = -1
            
            for i, item in enumerate(data):
                try:
                    if not isinstance(item, (list, tuple)) or len(item) < 5:
                        continue
                    
                    timestamp, _, _, _, _ = item
                    
                    # 시간 데이터 처리
                    if isinstance(timestamp, (int, float)):
                        if timestamp < 10000000000:  # 초 단위인 경우
                            dt = datetime.fromtimestamp(timestamp)
                        else:  # 밀리초 단위인 경우
                            dt = datetime.fromtimestamp(timestamp / 1000)
                    elif isinstance(timestamp, datetime):
                        dt = timestamp
                    else:
                        dt = datetime.now()
                    
                    # 현재 시간의 30분 단위 인덱스 계산 (예: 09:00~09:29 -> 18, 09:30~09:59 -> 19)
                    current_interval_index = (dt.hour * 60 + dt.minute) // 30
                    
                    label = ""
                    # 첫 데이터이거나, 새로운 30분 구간에 진입했을 때 레이블 표시
                    if last_interval_index != -1 and current_interval_index > last_interval_index:
                        label = dt.strftime("%H:%M")
                    elif last_interval_index == -1:
                         # 첫 데이터는 항상 표시하지 않고, 0분이나 30분에 가까울 때만 표시하거나
                         # 또는 그냥 첫 데이터도 표시 (여기서는 깔끔함을 위해 첫 데이터가 30분 단위에 가까우면 표시)
                         if dt.minute < 5 or dt.minute > 55 or (25 < dt.minute < 35):
                             label = dt.strftime("%H:%M")
                    
                    last_interval_index = current_interval_index
                    
                    if label:
                        tics.append((i, label))  # (X축 인덱스, 표시할 텍스트)
                        
                except Exception as e:
                    self.logger.debug(f"X축 레이블 설정 중 오류 (무시됨): {e}")
                    continue
            
            # pyqtgraph는 겹치는 레이블을 자동으로 숨겨 "..." 문제가 발생하지 않음
            if tics:
                axis.setTicks([tics])
                # 그리드 설정 (X축 눈금에 맞춰 세로선 표시)
                self.showGrid(x=True, y=False, alpha=0.3)
                self.logger.debug(f"🔍 PyQtGraphWidget X축 레이블 설정 완료: {len(tics)}개 레이블 ({chart_type} 차트)")
                
        except Exception as ex:
            self.logger.debug(f"X축 레이블 설정 중 오류 (무시됨): {ex}")
# ==================== PyQtGraph 기반 실시간 차트 위젯 ====================
class PyQtGraphRealtimeWidget(QWidget):
    
    """PyQtGraph 기반 실시간 차트 위젯 - 렌더링 전용"""
    def __init__(self, parent=None):
        self.logger = logging.getLogger(self.__class__.__name__)
        super().__init__(parent)
        self.parent_window = parent
        self.current_code = None
        self.chart_data = {'tics': [], 'minutes': []}
        
        # 성능 최적화 설정
        self.max_tic_data_points = 100  # 틱 데이터 최대 표시 수
        self.max_minute_data_points = 50  # 분봉 데이터 최대 표시 수
        self.update_batch_size = 20
        self.last_update_time = 0
        self.update_interval = 1.0  # 1초 간격 (실시간 업데이트)
        
        # 메모리 최적화를 위한 데이터 캐시
        self.data_cache = {'tics': [], 'minutes': []}
        self.cache_size = 100
        
        # 차트 위젯 초기화
        self.init_pyqtgraph_widgets()
        
        # 최적화된 타이머 설정 (UI 차트 렌더링용)
        self.chart_render_timer = QTimer()
        self.chart_render_timer.timeout.connect(self.optimized_update_charts)
        self.chart_render_timer.start(1000)  # 1초 간격 (실시간 업데이트)

        # 증분 업데이트를 위한 마지막 데이터 저장
        self.last_drawn_tic_datapoint = None
        self.last_drawn_min_datapoint = None
    
    def init_pyqtgraph_widgets(self):
        """PyQtGraph 위젯 초기화"""
        try:
            # 메인 레이아웃
            layout = QVBoxLayout()
            self.setLayout(layout)
            
            # 틱 차트 위젯
            self.tic_chart_widget = PyQtGraphWidget(parent=self, title="60틱 차트")
            self.tic_chart_widget.setMinimumHeight(200)  # 최소 높이 설정
            self.tic_chart_widget.setWindowFlags(Qt.WindowType.Widget)  # 독립 창 방지
            layout.addWidget(self.tic_chart_widget, 1)
            
            # 분봉 차트 위젯
            self.minute_chart_widget = PyQtGraphWidget(parent=self, title="3분봉 차트")
            self.minute_chart_widget.setMinimumHeight(200)  # 최소 높이 설정
            self.minute_chart_widget.setWindowFlags(Qt.WindowType.Widget)  # 독립 창 방지
            layout.addWidget(self.minute_chart_widget, 1)
            
            self.logger.debug("PyQtGraph 위젯 초기화 완료")
            
        except BaseException as ex:
            self.logger.error(f"❌ PyQtGraph 위젯 초기화 실패: {ex}", exc_info=True)
            traceback.print_exc()
    
    def set_current_code(self, code):
        """현재 종목 코드 설정 및 차트 데이터 로드"""
        self.logger.debug(f"🔍 PyQtGraph set_current_code 호출됨: {code}")
        
        if code != self.current_code:
            self.current_code = code

            # 차트 제목에 종목코드 추가
            if self.tic_chart_widget:
                self.tic_chart_widget.setTitle(f"60틱 차트 ({code})")
            if self.minute_chart_widget:
                self.minute_chart_widget.setTitle(f"3분봉 차트 ({code})")

            self.clear_charts()
            self.last_drawn_tic_datapoint = None
            self.last_drawn_min_datapoint = None
            self.logger.debug(f"📊 PyQtGraph 차트 종목 변경: {code}")
            
            # 종목 코드가 설정되면 캐시에서 차트 데이터 조회하여 차트 그리기
            if code and hasattr(self.parent_window, 'chart_cache') and self.parent_window.chart_cache:
                # self.logger.debug(f"🔍 PyQtGraph 차트 캐시 존재 확인됨, 데이터 조회 시작: {code}")
                loaded_from_db = False
                try:
                    cache_data = self.parent_window.chart_cache.get_cached_data(code)
                    self.logger.debug(f"🔍 PyQtGraph 캐시 데이터 조회 결과: {cache_data is not None}")
                    
                    if cache_data:
                        tic_data = cache_data.get('tic_data')
                        min_data = cache_data.get('min_data')
                        
                        self.logger.debug(f"🔍 PyQtGraph 캐시 데이터 구조: {list(cache_data.keys())}")
                        self.logger.debug(f"🔍 PyQtGraph 틱 데이터: {bool(tic_data)}, 분봉 데이터: {bool(min_data)}")
                        
                        if tic_data:
                            self.logger.debug(f"🔍 PyQtGraph 틱 데이터 타입: {type(tic_data)}")
                            if isinstance(tic_data, dict):
                                self.logger.debug(f"🔍 PyQtGraph 틱 데이터 키: {list(tic_data.keys())}")
                                if 'output' in tic_data:
                                    self.logger.debug(f"🔍 PyQtGraph 틱 output 길이: {len(tic_data['output']) if tic_data['output'] else 0}")
                            elif isinstance(tic_data, list):
                                self.logger.debug(f"🔍 PyQtGraph 틱 리스트 길이: {len(tic_data)}")
                        
                        if min_data:
                            self.logger.debug(f"🔍 PyQtGraph 분봉 데이터 타입: {type(min_data)}")
                            if isinstance(min_data, dict):
                                self.logger.debug(f"🔍 PyQtGraph 분봉 데이터 키: {list(min_data.keys())}")
                                if 'output' in min_data:
                                    self.logger.debug(f"🔍 PyQtGraph 분봉 output 길이: {len(min_data['output']) if min_data['output'] else 0}")
                            elif isinstance(min_data, list):
                                self.logger.debug(f"🔍 PyQtGraph 분봉 리스트 길이: {len(min_data)}")
                        
                        if tic_data or min_data:
                            self.logger.debug(f"🔍 PyQtGraph 차트 데이터 로드 및 그리기 시작: {code}")
                            # 제거된 update_chart_data 대신 직접 데이터 설정 및 그리기
                            if tic_data:
                                self.chart_data['tics'] = tic_data
                                self.last_drawn_tic_datapoint = self._get_last_datapoint(tic_data)
                            if min_data:
                                self.chart_data['minutes'] = min_data
                                self.last_drawn_min_datapoint = self._get_last_datapoint(min_data)
                            
                            # 차트 그리기
                            self.optimized_plot_charts()
                            self.logger.debug(f"📊 PyQtGraph 차트 데이터 로드 완료: {code}")
                        else:
                            self.logger.warning(f"⚠️ PyQtGraph 캐시에 차트 데이터가 없습니다. 빈 차트를 표시합니다: {code}")
                            # 캐시에 데이터가 없으면 차트를 비웁니다.
                            self.clear_charts()
                    else:
                        self.logger.warning(f"⚠️ PyQtGraph 캐시에서 차트 데이터를 찾을 수 없습니다: {code}")
                except Exception as ex:
                    self.logger.error(f"❌ PyQtGraph 차트 데이터 로드 실패: {code} - {ex}")
            elif code:
                self.logger.warning(f"⚠️ PyQtGraph 차트 캐시가 없어서 차트 데이터를 로드할 수 없습니다: {code}")
        else:
            self.logger.debug(f"🔍 PyQtGraph 동일한 종목 코드이므로 변경하지 않음: {code}")
    
    def clear_charts(self):
        """차트 데이터 초기화"""
        self.chart_data = {'tics': [], 'minutes': []}
        self.data_cache = {'tics': [], 'minutes': []}
        
        # 속성 존재 여부 확인 후 초기화
        if hasattr(self, 'tic_chart_widget') and self.tic_chart_widget is not None:
            self.tic_chart_widget.clear_chart()
        if hasattr(self, 'minute_chart_widget') and self.minute_chart_widget is not None:
            self.minute_chart_widget.clear_chart()
    
    def optimized_plot_charts(self, tic_data=None, min_data=None):
        """PyQtGraph 최적화된 차트 그리기"""
        try:
            # 인자로 받은 최신 데이터로 self.chart_data 업데이트
            if tic_data is not None:
                self.chart_data['tics'] = tic_data
            if min_data is not None:
                self.chart_data['minutes'] = min_data

            self.logger.debug(f"🔍 차트 그리기 시작 (tic: {self.chart_data.get('tics') is not None}, min: {self.chart_data.get('minutes') is not None})")
            
            # 틱 차트 그리기
            if self.chart_data.get('tics'):
                self._draw_pyqtchart_tic_chart()
            
            if self.chart_data.get('minutes'):
                self._draw_pyqtchart_minute_chart() # 인자 없이 호출하여 self.chart_data를 사용하도록 함
                
        except Exception as ex:
            self.logger.error(f"❌ PyQtGraph 차트 그리기 실패: {ex}", exc_info=True)
    
    def _draw_pyqtchart_tic_chart(self):
        """PyQtGraph 틱 차트 그리기"""
        try:
            # 위젯 초기화 확인
            if not hasattr(self, 'tic_chart_widget') or self.tic_chart_widget is None:
                self.logger.error("❌ PyQtGraph 틱 차트 위젯이 초기화되지 않았습니다")
                return
                
            self.logger.debug("🔍 PyQtGraph 틱 차트 그리기 시작")
            self.tic_chart_widget.clear_chart()
            
            # technical_indicators 변수 초기화
            if not hasattr(self, 'technical_indicators'):
                self.technical_indicators = {}
            
            # 틱 데이터 가져오기
            tic_data = self.chart_data.get('tics')
            if not tic_data:
                self.logger.warning("⚠️ PyQtGraph 틱 데이터가 없습니다")
                return
                
            # 데이터 처리 및 변환
            data_list = self._process_tic_data(tic_data)
            if not data_list:
                return
            
            # 차트 표시용 데이터 준비 (최대 100개)
            display_data = data_list[-100:] if len(data_list) > 100 else data_list
            self.logger.debug(f"🔍 틱 차트 데이터 처리: 표시 {len(display_data)}개")
            
            # 캔들스틱 데이터 생성
            candlestic_data = self._create_candlestic_data(display_data)
            if not candlestic_data:
                self.logger.warning("⚠️ 틱 차트 캔들스틱 데이터가 없습니다")
                return
            
            # 차트에 데이터 추가
            self.tic_chart_widget.add_candlestic_data(candlestic_data, chart_type="tic")
            self.logger.debug("✅ 틱 차트 캔들스틱 데이터 추가 완료")
            
            # 이동평균선 표시
            self._add_moving_averages_to_tic_chart(candlestic_data)
            
            # 차트 위젯 업데이트
            self.tic_chart_widget.update()
            self.tic_chart_widget.repaint()
            self.tic_chart_widget.update()
            self.tic_chart_widget.repaint()
            self.logger.debug("✅ 틱 차트 위젯 업데이트 완료")

            # 매입단가 표시 (보유 종목인 경우)
            self._add_buy_price_line(self.tic_chart_widget)
                                          
        except Exception as ex:
            self.logger.error(f"❌ PyQtGraph 틱 차트 그리기 실패: {ex}", exc_info=True)
    
    def _process_tic_data(self, tic_data):
        """틱 데이터 처리 및 변환"""
        if isinstance(tic_data, dict):
            if 'output' in tic_data and tic_data['output']:
                # API 응답 구조: {'output': [...]}
                data_list = tic_data['output']
                self._extract_moving_averages(tic_data)
            elif 'close' in tic_data and isinstance(tic_data.get('close'), list):
                # API 응답 구조: {'time': [...], 'open': [...], 'high': [...], 'low': [...], 'close': [...]}
                data_list = self._convert_list_to_dict_format(tic_data)
                self._extract_moving_averages(tic_data)
            elif 'time' in tic_data and 'close' in tic_data:
                # 단일 데이터
                data_list = [tic_data]
            else:
                # 기타 키 확인
                possible_keys = ['time', 'open', 'high', 'low', 'close', 'volume']
                if any(key in tic_data for key in possible_keys):
                    data_list = [tic_data]
                else:
                    self.logger.warning("⚠️ 틱 데이터에 필요한 키가 없음")
                    return None
        elif isinstance(tic_data, list):
            data_list = tic_data
        else:
            self.logger.warning(f"⚠️ 틱 데이터 형식이 예상과 다름: {type(tic_data)}")
            return None
            
        return data_list
    
    def _extract_moving_averages(self, tic_data):
        """이동평균선 데이터 추출"""
        ma_indicators = {}
        for key in ['MA5', 'MA20', 'MA60', 'MA120']:
            if key in tic_data and tic_data[key] is not None:
                ma_indicators[key] = tic_data[key]
        
        if ma_indicators:
            self.technical_indicators = ma_indicators
            self.logger.debug(f"✅ 이동평균선 데이터 추출 완료: {list(ma_indicators.keys())}")
        else:
            self.logger.warning("⚠️ 이동평균선 데이터를 찾을 수 없습니다")
    
    def _convert_list_to_dict_format(self, tic_data):
        """리스트 형식 데이터를 딕셔너리 리스트로 변환"""
        close_data = tic_data.get('close', [])
        time_data = tic_data.get('time', [])
        open_data = tic_data.get('open', [])
        high_data = tic_data.get('high', [])
        low_data = tic_data.get('low', [])
        volume_data = tic_data.get('volume', [0] * len(close_data))
        
        data_list = []
        for i in range(len(close_data)):
            close_price = close_data[i]
            
            # open, high, low 데이터가 없거나 0인 경우 close 값으로 대체
            open_price = open_data[i] if i < len(open_data) and open_data[i] != 0 else close_price
            high_price = high_data[i] if i < len(high_data) and high_data[i] != 0 else close_price
            low_price = low_data[i] if i < len(low_data) and low_data[i] != 0 else close_price
            
            # high는 close, open 중 최대값 이상이어야 함
            high_price = max(high_price, close_price, open_price)
            # low는 close, open 중 최소값 이하여야 함
            low_price = min(low_price, close_price, open_price)
            
            item = {
                'time': time_data[i] if i < len(time_data) else '',
                'open': open_price,
                'high': high_price,
                'low': low_price,
                'close': close_price,
                'volume': volume_data[i] if i < len(volume_data) else 0
            }
            data_list.append(item)
        
        self.logger.debug(f"🔍 API 응답 구조 변환: {len(data_list)}개 (OHLC 보정 완료)")
        return data_list
    
    def _create_candlestic_data(self, display_data):
        """캔들스틱 데이터 생성"""
        candlestic_data = []
        for i, item in enumerate(display_data):
            # 시간 변환
            timestamp = self._convert_time_to_timestamp(item.get('time', ''))
            
            # OHLC 데이터 추출
            open_price = safe_float_conversion(item.get('open', 0))
            high_price = safe_float_conversion(item.get('high', 0))
            low_price = safe_float_conversion(item.get('low', 0))
            close_price = safe_float_conversion(item.get('close', 0))
            
            candlestic_data.append((timestamp, open_price, high_price, low_price, close_price))
        
        return candlestic_data
    
    def _convert_time_to_timestamp(self, time_data):
        """시간 데이터를 타임스탬프로 변환"""
        if not time_data:
            return int(datetime.now().timestamp() * 1000)
        
        if isinstance(time_data, datetime):
            return int(time_data.timestamp() * 1000)
        elif isinstance(time_data, list) and time_data and isinstance(time_data[0], datetime):
            return int(time_data[0].timestamp() * 1000)
        elif isinstance(time_data, str):
            if len(time_data) == 14 and time_data.isdigit():
                # YYYYMMDDHHMMSS 형식
                try:
                    year = int(time_data[:4])
                    month = int(time_data[4:6])
                    day = int(time_data[6:8])
                    hour = int(time_data[8:10])
                    minute = int(time_data[10:12])
                    second = int(time_data[12:14])
                    dt = datetime(year, month, day, hour, minute, second)
                    return int(dt.timestamp() * 1000)
                except (ValueError, IndexError):
                    pass
            elif len(time_data) >= 6 and time_data[:6].isdigit():
                # HHMMSS 형식
                try:
                    hour = int(time_data[:2])
                    minute = int(time_data[2:4])
                    second = int(time_data[4:6])
                    today = datetime.now().date()
                    dt = datetime.combine(today, dt_time(hour, minute, second))
                    return int(dt.timestamp() * 1000)
                except (ValueError, IndexError):
                    pass
        elif isinstance(time_data, (int, float)):
            return float(time_data)
        
        # 기본값: 현재 시간
        return int(datetime.now().timestamp() * 1000)
    
    def _add_moving_averages_to_tic_chart(self, candlestic_data):
        """틱 차트에 이동평균선 추가"""
        if not hasattr(self, 'technical_indicators') or not self.technical_indicators:
            self.logger.warning("⚠️ technical_indicators 변수를 찾을 수 없습니다")
            return
        
        if not isinstance(self.technical_indicators, dict):
            self.logger.warning(f"⚠️ technical_indicators가 딕셔너리가 아닙니다: {type(self.technical_indicators)}")
            return
        
        ma_indicators = {}
        chart_length = len(candlestic_data)
        
        for key in ['MA5', 'MA20', 'MA60', 'MA120']:
            if key in self.technical_indicators and self.technical_indicators[key] is not None:
                full_ma_data = self.technical_indicators[key]
                ma_length = len(full_ma_data)
                
                if ma_length >= chart_length:
                    # 데이터가 충분한 경우: 차트 표시 범위에 맞게 슬라이스
                    sliced_ma_data = full_ma_data[-chart_length:]
                else:
                    # 데이터가 부족한 경우: 앞쪽에 NaN 추가하여 길이 맞춤
                    nan_padding = np.full(chart_length - ma_length, np.nan)
                    sliced_ma_data = np.concatenate([nan_padding, full_ma_data])
                
                ma_indicators[key] = sliced_ma_data
        
        if ma_indicators:
            self.tic_chart_widget.add_moving_averages(candlestic_data, ma_indicators, "tic")
            self.logger.debug(f"✅ 틱 차트 이동평균선 표시 완료: {list(ma_indicators.keys())}")
        else:
            self.logger.warning("⚠️ 이동평균선 데이터를 찾을 수 없습니다")
    
    def _draw_pyqtchart_minute_chart(self, minute_data=None):
        """PyQtGraph 분봉 차트 그리기"""
        try:
            # 위젯 초기화 확인
            if not hasattr(self, 'minute_chart_widget') or self.minute_chart_widget is None:
                self.logger.error("❌ PyQtGraph 분봉 차트 위젯이 초기화되지 않았습니다")
                return
            
            self.minute_chart_widget.clear_chart()
            
            # technical_indicators 변수 초기화
            if not hasattr(self, 'technical_indicators'): # 이 부분은 minute_chart 전용으로 분리될 수 있습니다.
                self.technical_indicators = {}
            
            # 분봉 데이터 가져오기
            if minute_data is None:
                minute_data = self.chart_data.get('minutes')
                
            if not minute_data:
                self.logger.warning("⚠️ PyQtGraph 분봉 데이터가 없습니다")
                return
            
            # 데이터 처리 및 변환
            data_list = self._process_minute_data(minute_data)
            if not data_list:
                return
            
            # 차트 표시용 데이터 준비 (최대 50개)
            display_data = data_list[-50:] if len(data_list) > 50 else data_list
            self.logger.debug(f"🔍 분봉 차트 데이터 처리: 표시 {len(display_data)}개")
            
            # 캔들스틱 데이터 생성
            candlestic_data = self._create_candlestic_data(display_data)
            if not candlestic_data:
                self.logger.warning("⚠️ 분봉 차트 캔들스틱 데이터가 없습니다")
                return
            
            # 차트에 데이터 추가
            self.minute_chart_widget.add_candlestic_data(candlestic_data, chart_type="minute")
            self.logger.debug("✅ 분봉 차트 캔들스틱 데이터 추가 완료")
            
            # 이동평균선 표시
            self._add_moving_averages_to_minute_chart(candlestic_data)
            
            # 차트 위젯 업데이트
            self.minute_chart_widget.update()
            self.minute_chart_widget.repaint()
            self.minute_chart_widget.update()
            self.minute_chart_widget.repaint()
            self.logger.debug("✅ 분봉 차트 위젯 업데이트 완료")

            # 매입단가 표시 (보유 종목인 경우)
            self._add_buy_price_line(self.minute_chart_widget)
                                          
        except Exception as ex:
            self.logger.error(f"❌ PyQtGraph 분봉 차트 그리기 실패: {ex}", exc_info=True)
    
    def _process_minute_data(self, minute_data):
        """분봉 데이터 처리 및 변환"""
        if isinstance(minute_data, dict):
            if 'output' in minute_data and minute_data['output']:
                # API 응답 구조: {'output': [...]}
                data_list = minute_data['output']
                self._extract_moving_averages_for_minute(minute_data)
            elif 'close' in minute_data and isinstance(minute_data.get('close'), list):
                # API 응답 구조: {'time': [...], 'open': [...], 'high': [...], 'low': [...], 'close': [...]}
                data_list = self._convert_list_to_dict_format(minute_data)
                self._extract_moving_averages_for_minute(minute_data)
            elif 'time' in minute_data and 'close' in minute_data:
                # 단일 데이터
                data_list = [minute_data]
            else:
                # 기타 키 확인
                possible_keys = ['time', 'open', 'high', 'low', 'close', 'volume']
                if any(key in minute_data for key in possible_keys):
                    data_list = [minute_data]
                else:
                    self.logger.warning("⚠️ 분봉 데이터에 필요한 키가 없음")
                    return None
        elif isinstance(minute_data, list):
            data_list = minute_data
        else:
            self.logger.warning(f"⚠️ 분봉 데이터 형식이 예상과 다름: {type(minute_data)}")
            return None
            
        return data_list
    
    def _extract_moving_averages_for_minute(self, minute_data):
        """분봉 차트용 이동평균선 데이터 추출"""
        ma_indicators = {}
        for key in ['MA5', 'MA10', 'MA20']:  # 분봉 차트용 이동평균선
            if key in minute_data and minute_data[key] is not None:
                ma_indicators[key] = minute_data[key]
        
        if ma_indicators:
            self.technical_indicators = ma_indicators
            self.logger.debug(f"✅ 분봉 이동평균선 데이터 추출 완료: {list(ma_indicators.keys())}")
        else:
            self.logger.warning("⚠️ 분봉 이동평균선 데이터를 찾을 수 없습니다")
    
    def _add_moving_averages_to_minute_chart(self, candlestic_data):
        """분봉 차트에 이동평균선 추가"""
        if not hasattr(self, 'technical_indicators') or not self.technical_indicators:
            self.logger.warning("⚠️ technical_indicators 변수를 찾을 수 없습니다")
            return
        
        if not isinstance(self.technical_indicators, dict):
            self.logger.warning(f"⚠️ technical_indicators가 딕셔너리가 아닙니다: {type(self.technical_indicators)}")
            return
        
        ma_indicators = {}
        chart_length = len(candlestic_data)
        
        for key in ['MA5', 'MA10', 'MA20']:
            if key in self.technical_indicators and self.technical_indicators[key] is not None:
                full_ma_data = self.technical_indicators[key]
                ma_length = len(full_ma_data)
                
                if ma_length >= chart_length:
                    # 데이터가 충분한 경우: 차트 표시 범위에 맞게 슬라이스
                    sliced_ma_data = full_ma_data[-chart_length:]
                else:
                    # 데이터가 부족한 경우: 앞쪽에 NaN 추가하여 길이 맞춤
                    nan_padding = np.full(chart_length - ma_length, np.nan)
                    sliced_ma_data = np.concatenate([nan_padding, full_ma_data])
                
                ma_indicators[key] = sliced_ma_data
        
        if ma_indicators:
            self.minute_chart_widget.add_moving_averages(candlestic_data, ma_indicators, "minute")
            self.logger.debug(f"✅ 분봉 차트 이동평균선 표시 완료: {list(ma_indicators.keys())}")
        else:
            self.logger.warning("⚠️ 이동평균선 데이터를 찾을 수 없습니다")
    
    def optimized_update_charts(self): # 증분 업데이트 로직
        """최적화된 차트 업데이트 (타이머에서 호출)"""
        if not self.current_code:
            return

        try:
            current_time = time.time()
            
            # 업데이트 간격 제한 (성능 최적화)
            if current_time - self.last_update_time < self.update_interval:
                return
                
            # 1. 캐시에서 최신 데이터 가져오기
            if hasattr(self.parent_window, 'chart_cache') and self.parent_window.chart_cache:
                cache_data = self.parent_window.chart_cache.get_cached_data(self.current_code)
                if cache_data:
                    tic_data = cache_data.get('tic_data')
                    min_data = cache_data.get('min_data')

                    # 2. 데이터 변경 여부 확인 및 증분 업데이트
                    tic_updated = self._is_data_updated(tic_data, self.last_drawn_tic_datapoint)
                    min_updated = self._is_data_updated(min_data, self.last_drawn_min_datapoint)

                    if tic_updated or min_updated:
                        self.last_update_time = current_time # type: ignore
                        # 변경된 데이터로 차트 다시 그리기
                        self.optimized_plot_charts(tic_data=tic_data, min_data=min_data)
                        # 마지막으로 그린 데이터 포인트 업데이트
                        self.last_drawn_tic_datapoint = self._get_last_datapoint(tic_data)
                        self.last_drawn_min_datapoint = self._get_last_datapoint(min_data)
                    
        except Exception as ex:
            self.logger.error(f"❌ 최적화된 차트 업데이트 실패: {ex}")

    def _is_data_updated(self, new_data, last_drawn_datapoint):
        """데이터가 업데이트되었는지 확인"""
        new_last_datapoint = self._get_last_datapoint(new_data)

        if not new_last_datapoint:
            return False
        
        if last_drawn_datapoint is None:
            return True # 처음 그리는 경우
        
        # 시간 비교
        new_time = new_last_datapoint.get('time')
        old_time = last_drawn_datapoint.get('time')
        if new_time > old_time:
            return True # 새로운 캔들 추가
        
        # 시간이 같으면 내용 비교 (마지막 캔들 업데이트)
        if new_time == old_time:
            if (new_last_datapoint.get('close') != last_drawn_datapoint.get('close') or
                new_last_datapoint.get('high') != last_drawn_datapoint.get('high') or
                new_last_datapoint.get('low') != last_drawn_datapoint.get('low')):
                return True
        
        return False

    def _get_last_datapoint(self, data):
        """데이터의 마지막 데이터 포인트를 딕셔너리로 반환"""
        if not data or 'time' not in data or not data['time']:
            return None
        
        last_index = -1
        last_datapoint = {}
        for key, values in data.items():
            if isinstance(values, list) and len(values) > 0:
                last_datapoint[key] = values[last_index]
        
        # 시간을 datetime 객체로 변환
        if 'time' in last_datapoint and isinstance(last_datapoint['time'], str):
            try:
                last_datapoint['time'] = datetime.strptime(last_datapoint['time'], '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                return None # 시간 파싱 실패 시
        
        return last_datapoint if 'time' in last_datapoint else None

    def clear_charts(self):
        """모든 차트 초기화"""
        try:
            self.current_code = None
            self.last_drawn_tic_datapoint = None
            self.last_drawn_min_datapoint = None
            
            if hasattr(self, 'tic_chart_widget'):
                self.tic_chart_widget.clear_chart()
                
            if hasattr(self, 'minute_chart_widget'):
                self.minute_chart_widget.clear_chart()
                
            self.logger.debug("✅ 실시간 차트 위젯 초기화 완료")
        except Exception as ex:
            self.logger.error(f"❌ 차트 초기화 실패: {ex}")




    def _add_buy_price_line(self, chart_widget):
        """차트에 매입단가 선 추가"""
        try:
            # 현재 종목이 보유 중인지 확인
            if not self.current_code:
                return

            buy_price = 0
            self.minute_chart_widget.repaint()
            self.minute_chart_widget.update()
            self.minute_chart_widget.repaint()
            self.logger.debug("✅ 분봉 차트 위젯 업데이트 완료")

            # 매입단가 표시 (보유 종목인 경우)
            self._add_buy_price_line(self.minute_chart_widget)
                                          
        except Exception as ex:
            self.logger.error(f"❌ PyQtGraph 분봉 차트 그리기 실패: {ex}", exc_info=True)
    
    def _process_minute_data(self, minute_data):
        """분봉 데이터 처리 및 변환"""
        if isinstance(minute_data, dict):
            if 'output' in minute_data and minute_data['output']:
                # API 응답 구조: {'output': [...]}
                data_list = minute_data['output']
                self._extract_moving_averages_for_minute(minute_data)
            elif 'close' in minute_data and isinstance(minute_data.get('close'), list):
                # API 응답 구조: {'time': [...], 'open': [...], 'high': [...], 'low': [...], 'close': [...]}
                data_list = self._convert_list_to_dict_format(minute_data)
                self._extract_moving_averages_for_minute(minute_data)
            elif 'time' in minute_data and 'close' in minute_data:
                # 단일 데이터
                data_list = [minute_data]
            else:
                # 기타 키 확인
                possible_keys = ['time', 'open', 'high', 'low', 'close', 'volume']
                if any(key in minute_data for key in possible_keys):
                    data_list = [minute_data]
                else:
                    self.logger.warning("⚠️ 분봉 데이터에 필요한 키가 없음")
                    return None
        elif isinstance(minute_data, list):
            data_list = minute_data
        else:
            self.logger.warning(f"⚠️ 분봉 데이터 형식이 예상과 다름: {type(minute_data)}")
            return None
            
        return data_list
    
    def _extract_moving_averages_for_minute(self, minute_data):
        """분봉 차트용 이동평균선 데이터 추출"""
        ma_indicators = {}
        for key in ['MA5', 'MA10', 'MA20']:  # 분봉 차트용 이동평균선
            if key in minute_data and minute_data[key] is not None:
                ma_indicators[key] = minute_data[key]
        
        if ma_indicators:
            self.technical_indicators = ma_indicators
            self.logger.debug(f"✅ 분봉 이동평균선 데이터 추출 완료: {list(ma_indicators.keys())}")
        else:
            self.logger.warning("⚠️ 분봉 이동평균선 데이터를 찾을 수 없습니다")
    
    def _add_moving_averages_to_minute_chart(self, candlestic_data):
        """분봉 차트에 이동평균선 추가"""
        if not hasattr(self, 'technical_indicators') or not self.technical_indicators:
            self.logger.warning("⚠️ technical_indicators 변수를 찾을 수 없습니다")
            return
        
        if not isinstance(self.technical_indicators, dict):
            self.logger.warning(f"⚠️ technical_indicators가 딕셔너리가 아닙니다: {type(self.technical_indicators)}")
            return
        
        ma_indicators = {}
        chart_length = len(candlestic_data)
        
        for key in ['MA5', 'MA10', 'MA20']:
            if key in self.technical_indicators and self.technical_indicators[key] is not None:
                full_ma_data = self.technical_indicators[key]
                ma_length = len(full_ma_data)
                
                if ma_length >= chart_length:
                    # 데이터가 충분한 경우: 차트 표시 범위에 맞게 슬라이스
                    sliced_ma_data = full_ma_data[-chart_length:]
                else:
                    # 데이터가 부족한 경우: 앞쪽에 NaN 추가하여 길이 맞춤
                    nan_padding = np.full(chart_length - ma_length, np.nan)
                    sliced_ma_data = np.concatenate([nan_padding, full_ma_data])
                
                ma_indicators[key] = sliced_ma_data
        
        if ma_indicators:
            self.minute_chart_widget.add_moving_averages(candlestic_data, ma_indicators, "minute")
            self.logger.debug(f"✅ 분봉 차트 이동평균선 표시 완료: {list(ma_indicators.keys())}")
        else:
            self.logger.warning("⚠️ 이동평균선 데이터를 찾을 수 없습니다")
    
    def optimized_update_charts(self): # 증분 업데이트 로직
        """최적화된 차트 업데이트 (타이머에서 호출)"""
        if not self.current_code:
            # self.logger.debug("⚠️ 차트 업데이트 건너뜀: 선택된 종목 없음")
            return

        try:
            current_time = time.time()
            
            # 업데이트 간격 제한 (성능 최적화)
            if current_time - self.last_update_time < self.update_interval:
                return
                
            # 1. 캐시에서 최신 데이터 가져오기
            if hasattr(self.parent_window, 'chart_cache') and self.parent_window.chart_cache:
                cache_data = self.parent_window.chart_cache.get_cached_data(self.current_code)
                if cache_data:
                    tic_data = cache_data.get('tic_data')
                    min_data = cache_data.get('min_data')

                    # 2. 데이터 변경 여부 확인 및 증분 업데이트
                    tic_updated = self._is_data_updated(tic_data, self.last_drawn_tic_datapoint)
                    min_updated = self._is_data_updated(min_data, self.last_drawn_min_datapoint)

                    if tic_updated or min_updated:
                        self.last_update_time = current_time # type: ignore
                        # 변경된 데이터로 차트 다시 그리기
                        self.optimized_plot_charts(tic_data=tic_data, min_data=min_data)
                        # 마지막으로 그린 데이터 포인트 업데이트
                        self.last_drawn_tic_datapoint = self._get_last_datapoint(tic_data)
                        self.last_drawn_min_datapoint = self._get_last_datapoint(min_data)
                    
        except Exception as ex:
            self.logger.error(f"❌ 최적화된 차트 업데이트 실패: {ex}")

    def _is_data_updated(self, new_data, last_drawn_datapoint):
        """데이터가 업데이트되었는지 확인"""
        new_last_datapoint = self._get_last_datapoint(new_data)

        if not new_last_datapoint:
            return False
        
        if last_drawn_datapoint is None:
            return True # 처음 그리는 경우
        
        # 시간 비교
        new_time = new_last_datapoint.get('time')
        old_time = last_drawn_datapoint.get('time')
        if new_time > old_time:
            return True # 새로운 캔들 추가
        
        # 시간이 같으면 내용 비교 (마지막 캔들 업데이트)
        if new_time == old_time:
            if (new_last_datapoint.get('close') != last_drawn_datapoint.get('close') or
                new_last_datapoint.get('high') != last_drawn_datapoint.get('high') or
                new_last_datapoint.get('low') != last_drawn_datapoint.get('low')):
                return True
        
        return False

    def _get_last_datapoint(self, data):
        """데이터의 마지막 데이터 포인트를 딕셔너리로 반환"""
        if not data or 'time' not in data or not data['time']:
            return None
        
        last_index = -1
        last_datapoint = {}
        for key, values in data.items():
            if isinstance(values, list) and len(values) > 0:
                last_datapoint[key] = values[last_index]
        
        # 시간을 datetime 객체로 변환
        if 'time' in last_datapoint and isinstance(last_datapoint['time'], str):
            try:
                last_datapoint['time'] = datetime.strptime(last_datapoint['time'], '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                return None # 시간 파싱 실패 시
        
        return last_datapoint if 'time' in last_datapoint else None

    def clear_charts(self):
        """모든 차트 초기화"""
        try:
            # self.current_code = None  <-- 이 줄을 제거하여 종목 코드 유지
            self.last_drawn_tic_datapoint = None
            self.last_drawn_min_datapoint = None
            
            if hasattr(self, 'tic_chart_widget'):
                self.tic_chart_widget.clear_chart()
                
            if hasattr(self, 'minute_chart_widget'):
                self.minute_chart_widget.clear_chart()
                
            self.logger.debug("✅ 실시간 차트 위젯 초기화 완료")
        except Exception as ex:
            self.logger.error(f"❌ 차트 초기화 실패: {ex}")




    def _add_buy_price_line(self, chart_widget):
        """차트에 매입단가 선 추가"""
        try:
            # 현재 종목이 보유 중인지 확인
            if not self.current_code:
                return

            buy_price = 0
            is_held = False
            
            # 1. 부모 윈도우의 trader를 통해 보유 정보 확인
            if hasattr(self.parent_window, 'trader') and self.parent_window.trader:
                portfolio = self.parent_window.trader.get_portfolio_status()
                if self.current_code in portfolio['holdings']:
                    is_held = True
                    buy_price = portfolio['buy_prices'].get(self.current_code, 0)
                    # self.logger.debug(f"🔍 [Trader] 보유 확인: {self.current_code}, 매입가: {buy_price}")

            # 2. Fallback: LoginHandler의 balance_data 직접 확인 (Trader 동기화 지연 대비)
            if not is_held and hasattr(self.parent_window, 'login_handler'):
                lh = self.parent_window.login_handler
                if hasattr(lh, 'websocket_client') and lh.websocket_client:
                    bd = getattr(lh.websocket_client, 'balance_data', {})
                    if self.current_code in bd:
                        is_held = True
                        buy_price = bd[self.current_code].get('average_price', 0)
                        self.logger.debug(f"🔍 [Fallback] 웹소켓 데이터에서 보유 확인: {self.current_code}, 매입가: {buy_price}")
            
            # 보유 중이고 매입가가 유효하면 선 그리기
            if is_held and buy_price > 0:
                # 기존 매입단가 선 제거 (InfiniteLine 타입 찾아서 제거)
                # 주의: 다른 용도의 InfiniteLine이 있다면 태그를 확인해야 함
                for item in chart_widget.plotItem.items[:]:
                    if isinstance(item, pg.InfiniteLine):
                        # 매입단가 선인지 확인 (태그 또는 라벨)
                        if getattr(item, 'is_buy_price_line', False) or (item.label and "매입가" in item.label.text):
                            chart_widget.removeItem(item)
                        # 태그가 없는 경우도 일단 제거 (이전 버전 호환성)
                        elif not hasattr(item, 'is_buy_price_line'):
                             chart_widget.removeItem(item)
                
                # 매입단가 선 추가
                # 잘 보이는 마젠타색 점선으로 변경 (두께 2)
                pen = pg.mkPen(color=(255, 0, 255), width=2, style=Qt.PenStyle.DashLine) 
                line = chart_widget.add_infinite_line(
                    pos=buy_price, 
                    angle=0, 
                    pen=pen, 
                    label=f"매입가: {buy_price:,.0f}", 
                    labelOpts={'position': 0.1, 'color': (255, 0, 255), 'movable': True, 'fill': (255, 255, 255, 200)}
                )
                
                if line:
                    line.is_buy_price_line = True  # 식별 태그 추가
                    
                    # Y축 범위 조정 (매입가가 화면 밖이면 포함하도록)
                    vb = chart_widget.plotItem.getViewBox()
                    if vb:
                        min_y, max_y = vb.viewRange()[1]
                        
                        # 매입가가 범위 밖이거나 너무 경계에 있는 경우 조정
                        if buy_price < min_y or buy_price > max_y:
                            padding = (max_y - min_y) * 0.2  # 20% 여백
                            new_min = min(min_y, buy_price - padding)
                            new_max = max(max_y, buy_price + padding)
                            
                            chart_widget.setYRange(new_min, new_max)
                            self.logger.debug(f"🔍 Y축 범위 조정: {min_y:.0f}~{max_y:.0f} -> {new_min:.0f}~{new_max:.0f} (매입가 {buy_price:.0f} 포함)")
                    
                    self.logger.debug(f"✅ {chart_widget.plotItem.titleLabel.text} 매입단가 선 표시: {buy_price:,.0f}원")
                else:
                    self.logger.error(f"❌ 매입단가 선 객체 생성 실패")
            else:
                # 보유 중이 아니면 기존 선 제거
                for item in chart_widget.plotItem.items[:]:
                    if isinstance(item, pg.InfiniteLine):
                        if getattr(item, 'is_buy_price_line', False) or (item.label and "매입가" in item.label.text):
                            chart_widget.removeItem(item)
                        elif not hasattr(item, 'is_buy_price_line'):
                             chart_widget.removeItem(item)

        except Exception as ex:
            self.logger.error(f"❌ 매입단가 선 표시 실패: {ex}", exc_info=True)
