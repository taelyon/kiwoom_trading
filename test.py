# 표준 라이브러리
import random
import sys
import time

# 서드파티 라이브러리
import numpy as np
import pyqtgraph as pg

# PyQt6 관련
from PyQt6.QtCore import QDateTime
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QMainWindow, QRadioButton, QVBoxLayout, QWidget
)

# -----------------------------------------------------------------
# 1. pyqtgraph 예제에 포함된 CandlestickItem 클래스
# (pyqtgraph에 내장된 클래스가 아니므로, 코드에 직접 포함해야 합니다)
# -----------------------------------------------------------------
class CandlestickItem(pg.GraphicsObject):
    def __init__(self, data):
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
        # QRect를 QRectF로 변환
        rect = self.picture.boundingRect()
        return pg.QtCore.QRectF(rect)


# -----------------------------------------------------------------
# 2. 메인 윈도우
# -----------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("pyqtgraph Candlestick Chart")
        self.setGeometry(100, 100, 1000, 600)

        # 기본 위젯 및 레이아웃
        self.central_widget = QWidget()
        self.layout = QVBoxLayout(self.central_widget)
        self.setCentralWidget(self.central_widget)

        # --- 차트 모드 선택 라디오 버튼 ---
        self.radio_layout = QHBoxLayout()
        self.radio_date_axis = QRadioButton("방법 1: DateAxisItem (시간축 O, 간격 O)")
        self.radio_index_axis = QRadioButton("방법 2: Index-based Axis (시간축 X, 간격 X)")
        self.radio_layout.addWidget(self.radio_date_axis)
        self.radio_layout.addWidget(self.radio_index_axis)
        self.layout.addLayout(self.radio_layout)
        
        self.radio_date_axis.setChecked(True)
        self.radio_date_axis.toggled.connect(self.update_chart_mode)

        # --- pyqtgraph 플롯 위젯 생성 ---
        self.plot_widget = None # 모드에 따라 재생성
        
        # --- 데이터 생성 ---
        self.generate_data()

        # --- 차트 초기화 ---
        self.update_chart_mode()

    def generate_data(self):
        """시뮬레이션 데이터 생성"""
        self.timestamps = []
        self.ohlc_data = []
        
        now = QDateTime.currentDateTime()
        current_time = now.addDays(-1).toSecsSinceEpoch() # 하루 전부터 시작
        last_close = 10000
        
        for i in range(500):
            # 불규칙한 시간 간격 (1초 ~ 300초 사이)
            current_time += random.randint(1, 300)
            
            # 10:00 ~ 10:30 사이 데이터 공백 시뮬레이션
            dt_obj = QDateTime.fromSecsSinceEpoch(int(current_time))
            if dt_obj.time().hour() == 10 and dt_obj.time().minute() < 30:
                continue

            o = last_close + random.uniform(-50, 50)
            h = max(o, last_close) + random.uniform(0, 50)
            l = min(o, last_close) - random.uniform(0, 50)
            c = o + random.uniform(-50, 50)
            last_close = c

            self.timestamps.append(current_time)
            self.ohlc_data.append((o, h, l, c))

    def update_chart_mode(self):
        """차트 모드 변경 시 호출"""
        
        # 기존 플롯 위젯 제거
        if self.plot_widget:
            self.layout.removeWidget(self.plot_widget)
            self.plot_widget.deleteLater()


        self.setup_index_axis_chart()



    def setup_index_axis_chart(self):
        """방법 2: Index-based Axis (수동 30분 레이블) 사용"""
        
        # 1. 기본 PlotWidget 생성
        self.plot_widget = pg.PlotWidget(
             title="방법 2: Index-based Axis (시간축 X, 간격 X)"
        )
        self.plot_widget.showGrid(x=True, y=True, alpha=0.5)
        self.layout.addWidget(self.plot_widget)

        # 2. (index, o, h, l, c) 데이터 준비 (X축을 타임스탬프 대신 인덱스로)
        data_list = []
        for i, (o, h, l, c) in enumerate(self.ohlc_data):
            # X축 값으로 timestamp 대신 인덱스(i) 사용
            data_list.append((i, o, h, l, c)) 
            
        np_data = np.array(data_list)
        
        # 3. CandlestickItem 생성 및 추가
        self.candle_item = CandlestickItem(np_data)
        self.plot_widget.addItem(self.candle_item)

        # 4. X축 레이블 수동 설정 (PyQtChart의 QBarCategoryAxis와 동일한 방식)
        axis = self.plot_widget.getAxis('bottom')
        
        ticks = [] # (index, "label") 튜플의 리스트
        last_label_minute = -1
        
        for i, ts in enumerate(self.timestamps):
            dt = QDateTime.fromSecsSinceEpoch(int(ts))
            minute = dt.time().minute()
            
            label = ""
            if (minute == 0 or minute == 30) and minute != last_label_minute:
                last_label_minute = minute
                label = dt.toString("hh:mm")
            elif minute != 0 and minute != 30:
                last_label_minute = -1
            
            if label:
                ticks.append((i, label)) # (X축 인덱스, 표시할 텍스트)

        # pyqtgraph는 겹치는 레이블을 자동으로 숨겨 "..." 문제가 발생하지 않음
        axis.setTicks([ticks]) 
        
        print("방법 2: 모든 봉이 동일한 간격이며 30분 단위 레이블만 표시됩니다.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    # pyqtgraph 전역 설정 (배경/전경)
    pg.setConfigOption('background', 'w') # 'w' = white
    pg.setConfigOption('foreground', 'k') # 'k' = black
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec())