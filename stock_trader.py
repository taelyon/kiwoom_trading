"""
키움 REST API 기반 자동매매 프로그램
크레온 플러스 API를 키움 REST API로 전면 리팩토링
"""

# 표준 라이브러리
import asyncio
import ast
import ctypes
import gc
import json
import logging
import os
import queue
import sqlite3
import sys
import threading
import time
import traceback
import warnings
from collections import deque
from datetime import datetime, timedelta, time as dt_time
from threading import Lock
from typing import Dict, List, Optional, Any

# pyqtgraph가 PyQt6를 사용하도록 환경변수 설정 (PyQt5 충돌 방지)
os.environ['PYQTGRAPH_QT_LIB'] = 'PyQt6'

# 서드파티 라이브러리
import aiofiles
import aiosqlite
import configparser
import concurrent.futures
import io
import numpy as np
import pandas as pd
import pyqtgraph as pg
import qasync
from qasync import asyncSlot
import httpx
import talib
import websockets

# 프로젝트 내부 모듈
import strategy_utils
# from chart_pyqtgraph import PyQtGraphRealtimeWidget  # 파일 없음 - 주석 처리
from backtester import KiwoomBacktester
# from chart_data_cache import ChartDataCache  # 파일 없음 - 주석 처리

# PyQt6 관련
from PyQt6.QtCore import (
    QCoreApplication, QDateTime, QEventLoop, QMetaType, QObject, QPointF, QThread, QTimer, Qt,
    pyqtSignal, pyqtSlot, QRunnable, QThreadPool
)
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QIcon, QPainter, QPen, QPicture, QTextCursor
)
from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
from PyQt6.QtWidgets import *
from pyqtgraph import LegendItem

# PyQt6 설정
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

# Python 3.12 datetime adapter 경고 해결
def adapt_datetime_iso(val):
    """datetime을 ISO 형식 문자열로 변환"""
    return val.isoformat()

def convert_datetime(val):
    """ISO 형식 문자열을 datetime으로 변환"""
    return datetime.fromisoformat(val.decode())

# sqlite3 datetime adapter 등록
sqlite3.register_adapter(datetime, adapt_datetime_iso)
sqlite3.register_converter("datetime", convert_datetime)

# PyQtGraph 설정
pg.setConfigOption('background', 'w')  # 배경색을 흰색으로 설정
pg.setConfigOption('foreground', 'k')  # 전경색을 검은색으로 설정

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# QTextEdit 삭제 오류 방지를 위한 추가 설정

IS_WINDOWS = sys.platform.startswith('win')

def _prevent_system_sleep():
    """Windows 환경에서만 동작하는 절전 모드 해제 처리"""
    if not IS_WINDOWS or not hasattr(ctypes, "windll"):
        return

    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception as ex:
        logging.warning(f"시스템 절전 방지 설정 실패: {ex}")

_prevent_system_sleep()
os.environ['QT_AUTO_SCREEN_SCALE_FACTOR'] = '1'
os.environ['QT_SCALE_FACTOR'] = '1'

def get_resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)


def safe_float_conversion(value, default=0.0):
    """
    안전한 float 변환 함수 (통합 버전)
    
    Args:
        value: 변환할 값 (int, float, str, list 등)
        default: 변환 실패 시 반환할 기본값
        
    Returns:
        float: 변환된 값 또는 기본값
    """
    # None 또는 빈 문자열 체크
    if value is None or value == '':
        return default
    
    try:
        # 리스트인 경우 첫 번째 요소 사용
        if isinstance(value, list):
            if len(value) > 0:
                return float(value[0])
            else:
                return default
        
        # 문자열인 경우 공백 제거
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return default
        
        # int, float, str 변환
        return float(value)
        
    except (ValueError, TypeError) as ex:
        # float 변환 실패 로그 제거 (너무 빈번함)
        return default

def setup_logging():
    """로그 설정"""
    try:
        # 로그 디렉토리 생성
        log_dir = "logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        # 로그 파일명 (날짜별)
        log_filename = f"{log_dir}/kiwoom_trader_{datetime.now().strftime('%Y%m%d')}.log"
        
        # 로그 포맷
        log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        formatter = logging.Formatter(log_format)
        
        # root 로거 설정 (DEBUG 레벨로 설정하여 모든 로그 받기)
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        
        # 기존 핸들러 제거
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # 파일 핸들러 (DEBUG 레벨 - 모든 로그 저장)
        file_handler = logging.FileHandler(log_filename, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        
        # 콘솔/터미널 핸들러 (DEBUG 레벨 - 개발 시 상세 로그 확인용)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
        
        # aiosqlite DEBUG 로그 비활성화
        aiosqlite_logger = logging.getLogger('aiosqlite')
        aiosqlite_logger.setLevel(logging.WARNING)
        
        # qasync DEBUG 로그 비활성화
        qasync_logger = logging.getLogger('qasync')
        qasync_logger.setLevel(logging.WARNING)
        
        # websockets.client DEBUG 로그 비활성화
        websockets_logger = logging.getLogger('websockets.client')
        websockets_logger.setLevel(logging.WARNING)
        
        # urllib3.connectionpool DEBUG 로그 비활성화
        urllib3_logger = logging.getLogger('urllib3.connectionpool')
        urllib3_logger.setLevel(logging.WARNING)

        # httpx DEBUG 로그 비활성화
        httpx_logger = logging.getLogger('httpx')
        httpx_logger.setLevel(logging.WARNING)

        # httpcore.http11 DEBUG 로그 비활성화
        httpcore_logger = logging.getLogger('httpcore.http11')
        httpcore_logger.setLevel(logging.WARNING)

        # UI 로그 핸들러 추가 (INFO 레벨)
        # MyWindow 인스턴스가 생성된 후에 호출되어야 함
        if 'main_window' in globals() and globals()['main_window']:
            # UI 로그창 전용 포맷터
            ui_log_format = '%(asctime)s - %(message)s'
            ui_formatter = logging.Formatter(ui_log_format, datefmt='%H:%M:%S')
            text_edit_logger = QTextEditLogger(globals()['main_window'].trading_tab.terminalOutput)
            text_edit_logger.setLevel(logging.INFO)
            text_edit_logger.setFormatter(ui_formatter)
            root_logger.addHandler(text_edit_logger)
                
    except Exception as ex:
        print(f"로깅 설정 실패: {ex}")

# ==================== API 제한 관리 ====================
class ApiLimitManager:
    """API 제한 관리 클래스 (개선된 버전)"""
    logger = logging.getLogger(__qualname__)
    
    # API 요청 간격 관리 (초 단위)
    _last_request_time = {}
    _request_intervals = {
        'tic_chart': 1.5,    # 틱 차트: 1.5초 간격 (429 에러 방지)
        'order': 0.5,         # 주문: 0.5초 간격
        'minute_chart': 1.5,  # 분봉 차트: 1.5초 간격 (429 에러 방지)
        'tic': 0.5,           # 틱 데이터: 0.5초 간격
        'deposit': 1.0,       # 예수금 조회: 1초 간격
        'minute': 0.5,        # 분봉 데이터: 0.5초 간격
        'default': 0.5        # 기본: 0.5초 간격
    }
    
    @classmethod
    def _get_request_type(cls, operation_name):
        """요청 타입 결정"""
        if '틱' in operation_name or 'tic' in operation_name.lower():
            return 'tic_chart'
        elif '분봉' in operation_name or 'minute' in operation_name.lower():
            return 'minute_chart'
        elif '주문' in operation_name:
            return 'order'
        elif '예수금' in operation_name:
            return 'deposit'
        else:
            return 'default'
    
    @classmethod
    async def check_api_limit_and_wait_async(cls, operation_name="API 요청", rqtype=0, request_type=None):
        """API 제한 확인 및 대기 (비동기 버전)"""
        try:
            # 요청 타입별 간격 설정
            if request_type is None:
                request_type = cls._get_request_type(operation_name)
            interval = cls._request_intervals.get(request_type, cls._request_intervals['default'])
            
            # 마지막 요청 시간 확인
            current_time = time.time()
            last_time = cls._last_request_time.get(request_type, 0)
            
            # 필요한 대기 시간 계산
            elapsed_time = current_time - last_time
            if elapsed_time < interval:
                wait_time = interval - elapsed_time
                # 비동기 대기 (qasync 환경에서 안전)
                await asyncio.sleep(wait_time)
            
            # 요청 시간 업데이트
            cls._last_request_time[request_type] = time.time()
            return True
            
        except Exception as ex:
            cls.logger.error(f"API 제한 확인 중 오류: {ex}")
            return False

# ==================== 로그 핸들러 ====================
class QTextEditLogger(logging.Handler):
    """QTextEdit에 로그를 출력하는 핸들러 (스레드 안전)"""
    
    # 이 클래스는 로깅 핸들러 자체이므로 self.logger를 사용하지 않습니다.
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget
        
    def emit(self, record):
        try:
            # QTextEdit 위젯이 유효한지 더 강화된 검사
            if not self.text_widget or not hasattr(self, 'text_widget'):
                # 핸들러 자체를 로거에서 제거
                logging.getLogger().removeHandler(self)
                return
                
            # 위젯이 삭제되었는지 확인
            try:
                if not hasattr(self.text_widget, 'append'):
                    return
                # 위젯이 삭제되었는지 확인 (isVisible() 호출 시 RuntimeError 발생 가능)
                self.text_widget.isVisible()
            except (RuntimeError, AttributeError):
                # 위젯이 삭제된 경우
                return
                
            msg = self.format(record)
            
            # 스레드 안전한 텍스트 추가
            try:
                # QTextEdit이 여전히 유효한지 다시 확인
                if hasattr(self.text_widget, 'append'):
                    self.text_widget.append(msg)
                
                # 스크롤은 안전하게 처리
                try:
                    if hasattr(self.text_widget, 'verticalScrollBar'):
                        scrollbar = self.text_widget.verticalScrollBar()
                        if scrollbar and scrollbar.isVisible():
                            max_val = scrollbar.maximum()
                            if max_val > 0:
                                scrollbar.setValue(max_val)
                except (RuntimeError, AttributeError):
                    # 스크롤 실패 시 무시
                    pass
                    
            except (RuntimeError, AttributeError):
                # 텍스트 추가 실패 시 무시 (위젯이 삭제된 경우)
                pass
                
        except Exception:
            # 로그 핸들러에서 예외가 발생하면 무시 (무한 루프 방지)
            pass

# ==================== 데이터베이스 관리 ====================
class AsyncDatabaseManager:
    """비동기 데이터베이스 관리 클래스 (I/O 바운드 작업)"""
    
    def __init__(self, db_path="stock_data.db"):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.db_path = db_path
        self.indicator_list = [
            'MA5', 'MA10', 'MA20', 'MA50', 'MA60', 'MA120', 'RSI', 'MACD', 'MACD_SIGNAL', 'MACD_HIST',
            'BB_UPPER', 'BB_MIDDLE', 'BB_LOWER', 'STOCH_K', 'STOCH_D', 'WILLIAMS_R', 'ROC', 'OBV', 'OBV_MA20', 'ATR'
        ]
        # 비동기 초기화는 별도로 호출해야 함
        # self.init_database()  # 비동기 메서드이므로 직접 호출 불가
    
    async def init_database(self):
        """데이터베이스 초기화 (비동기 I/O)"""
        try:
            
            async with aiosqlite.connect(self.db_path, timeout=10.0) as conn:
                cursor = await conn.cursor()
            
            # stock_data 테이블은 생성하지 않음 (틱 데이터와 분봉 데이터만 사용)
            
            # 매매 기록 테이블
                await cursor.execute('''
                CREATE TABLE IF NOT EXISTS trade_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    datetime TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    quantity INTEGER,
                    price REAL,
                    amount REAL,
                    strategy TEXT,
                    profit_loss REAL DEFAULT 0
                )
            ''')
            
                # 통합 주식 데이터 테이블 동적 생성
                tic_indicator_cols = ", ".join([f"tic_{col.lower()} REAL" for col in self.indicator_list])
                min_indicator_cols = ", ".join([f"min3_{col.lower()} REAL" for col in self.indicator_list])
                
                create_table_sql = f'''
                    CREATE TABLE IF NOT EXISTS stock_data (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT NOT NULL,
                        datetime TEXT NOT NULL,
                        -- 틱봉 데이터
                        tic_open REAL,
                        tic_high REAL,
                        tic_low REAL,
                        tic_close REAL,
                        tic_volume INTEGER,
                        tic_strength REAL,
                        -- 기술적 지표 (틱봉)
                        {tic_indicator_cols},
                        -- 기술적 지표 (분봉)
                        {min_indicator_cols},
                        created_at TEXT,
                        UNIQUE(code, datetime)
                    )
                '''
                await cursor.execute(create_table_sql)
                
                await conn.commit()
            
            # 데이터베이스 초기화 로그 제거
            
        except Exception as ex:
            self.logger.error(f"데이터베이스 초기화 실패: {ex}")
            raise ex
    
    async def save_stock_data(self, code, tic_data, min_data):
        """통합 주식 데이터 저장 (틱봉 기준, 분봉 데이터 포함)"""
        try:
            if not tic_data or not min_data:
                return
            
            async with aiosqlite.connect(self.db_path, timeout=10.0) as conn:
                cursor = await conn.cursor()
                
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                # 틱봉 데이터 기준으로 저장
                tic_times = tic_data.get('time', [])
                tic_opens = tic_data.get('open', [])
                tic_highs = tic_data.get('high', [])
                tic_lows = tic_data.get('low', [])
                tic_closes = tic_data.get('close', [])
                tic_volumes = tic_data.get('volume', [])
                tic_strengths = tic_data.get('strength', [])

                # 실제 캐시 데이터에서 기술적 지표 키 추출 (OHLCV 제외)
                basic_keys = {'time', 'open', 'high', 'low', 'close', 'volume', 'strength'}
                tic_indicators = [key for key in tic_data.keys() if key not in basic_keys]
                min_indicators = [key for key in min_data.keys() if key not in basic_keys]
                
                # 모든 지표 통합 (중복 제거) # type: ignore
                all_indicators = list(set(tic_indicators + min_indicators))
                all_indicators.sort()  # 정렬하여 일관성 유지
                
                logging.debug(f"📊 {code}: 감지된 기술적 지표 - 틱봉: {tic_indicators}, 분봉: {min_indicators}, 통합: {all_indicators}")
                
                # 테이블 스키마 동적 업데이트
                await self._ensure_table_schema(cursor, all_indicators)

                # 동적으로 컬럼명과 플레이스홀더 생성
                tic_indicator_cols = ", ".join([f"tic_{col.lower()}" for col in all_indicators])
                min_indicator_cols = ", ".join([f"min3_{col.lower()}" for col in all_indicators])
                
                columns = (
                    "code, datetime, tic_open, tic_high, tic_low, tic_close, tic_volume, tic_strength, "
                    f"{tic_indicator_cols}, {min_indicator_cols}, created_at"
                )
                
                placeholders = ", ".join(["?"] * (9 + len(all_indicators) * 2))

                sql = f"INSERT OR REPLACE INTO stock_data ({columns}) VALUES ({placeholders})"
                
                # 틱봉 데이터 개수만큼 저장
                for i in range(len(tic_times)):
                    # 해당 시점의 분봉 데이터 찾기 (시간 기준으로 매칭)
                    min_idx = self._find_matching_minute_data(tic_times[i], min_data.get('time', []))
                    
                    # datetime 객체를 일반 형식으로 변환
                    datetime_str = tic_times[i].strftime('%Y-%m-%d %H:%M:%S') if hasattr(tic_times[i], 'strftime') else str(tic_times[i])
                    
                    values = [
                        code,
                        datetime_str,
                        # 틱봉 데이터
                        tic_opens[i] if i < len(tic_opens) else 0,
                        tic_highs[i] if i < len(tic_highs) else 0,
                        tic_lows[i] if i < len(tic_lows) else 0,
                        tic_closes[i] if i < len(tic_closes) else 0,
                        tic_volumes[i] if i < len(tic_volumes) else 0,
                        tic_strengths[i] if i < len(tic_strengths) else 0,
                    ]

                    # 틱봉 기술적 지표 값 추가
                    for indicator in all_indicators:
                        try:
                            indicator_data = tic_data.get(indicator, [])
                            
                            # 배열인 경우 특정 인덱스 접근
                            if isinstance(indicator_data, (list, tuple, np.ndarray)):
                                if i < len(indicator_data):
                                    value = indicator_data[i]
                                    # numpy scalar 변환
                                    if isinstance(value, np.generic):
                                        value = value.item()
                                    # NaN이 아닌 경우에만 추가
                                    if not pd.isna(value):
                                        values.append(value)
                                    else:
                                        values.append(None)
                                else:
                                    values.append(None)
                            else:
                                # 단일 값인 경우
                                value = indicator_data
                                # numpy scalar 변환
                                if isinstance(value, np.generic):
                                    value = value.item()
                                # NaN이 아닌 경우에만 추가
                                if not pd.isna(value):
                                    values.append(value)
                                else:
                                    values.append(None)
                        except Exception as ex:
                            self.logger.debug(f"틱봉 지표 처리 중 오류 ({indicator}): {ex}")
                            values.append(None)

                    # 분봉 기술적 지표 값 추가
                    for indicator in all_indicators:
                        try:
                            indicator_data = min_data.get(indicator, [])
                            
                            # 배열인 경우 특정 인덱스 접근
                            if isinstance(indicator_data, (list, tuple, np.ndarray)):
                                if min_idx >= 0 and min_idx < len(indicator_data):
                                    value = indicator_data[min_idx]
                                    # numpy scalar 변환
                                    if isinstance(value, np.generic):
                                        value = value.item()
                                    # NaN이 아닌 경우에만 추가
                                    if not pd.isna(value):
                                        values.append(value)
                                    else:
                                        values.append(None)
                                else:
                                    values.append(None)
                            else:
                                # 단일 값인 경우
                                value = indicator_data
                                # numpy scalar 변환
                                if isinstance(value, np.generic):
                                    value = value.item()
                                # NaN이 아닌 경우에만 추가
                                if not pd.isna(value):
                                    values.append(value)
                                else:
                                    values.append(None)
                        except Exception as ex:
                            self.logger.debug(f"분봉 지표 처리 중 오류 ({indicator}): {ex}")
                            values.append(None)
                    
                    values.append(current_time)

                    await cursor.execute(sql, tuple(values))
                
                await conn.commit()
                # 데이터 저장 완료 로그 제거 (너무 빈번함)
                
        except Exception as ex:
            self.logger.error(f"통합 주식 데이터 저장 실패 ({code}): {ex}", exc_info=True)
    
    async def _ensure_table_schema(self, cursor, indicators):
        """테이블 스키마에 필요한 컬럼들이 있는지 확인하고 없으면 추가"""
        try:
            # 기존 테이블의 컬럼 정보 조회
            await cursor.execute("PRAGMA table_info(stock_data)")
            existing_columns = [row[1] for row in await cursor.fetchall()]
            
            # 새로 추가할 컬럼들 확인
            new_columns = []
            for indicator in indicators:
                tic_col = f"tic_{indicator.lower()}"
                min_col = f"min3_{indicator.lower()}"
                
                if tic_col not in existing_columns:
                    new_columns.append(tic_col)
                if min_col not in existing_columns: # min_ -> min3_
                    new_columns.append(min_col)
            
            # 새 컬럼들 추가
            for col in new_columns:
                try:
                    await cursor.execute(f"ALTER TABLE stock_data ADD COLUMN {col} REAL")
                    self.logger.debug(f"📊 새 컬럼 추가: {col}")
                except Exception as e:
                    # 컬럼이 이미 존재하는 경우 무시
                    if "duplicate column name" not in str(e).lower():
                        self.logger.warning(f"⚠️ 컬럼 추가 실패 ({col}): {e}")
                        
        except Exception as ex:
            self.logger.error(f"❌ 테이블 스키마 확인/업데이트 실패: {ex}", exc_info=True)
    
    def _find_matching_minute_data(self, tic_time, min_times):
        """틱봉 시간에 해당하는 분봉 데이터 인덱스 찾기 (가장 가까운 분봉 찾기)"""
        try:
            if not min_times:
                return -1
                
            # tic_time이 datetime 객체인지 문자열인지 확인
            if hasattr(tic_time, 'strftime'):
                # datetime 객체인 경우
                tic_dt = tic_time
            else:
                # 문자열인 경우 파싱
                tic_dt = datetime.strptime(str(tic_time), '%Y-%m-%d %H:%M:%S')
            
            best_match_idx = -1
            min_time_diff = float('inf')
            
            for i, min_time in enumerate(min_times):
                # min_time도 datetime 객체인지 문자열인지 확인
                if hasattr(min_time, 'strftime'):
                    # datetime 객체인 경우
                    min_dt = min_time
                else:
                    # 문자열인 경우 파싱
                    min_dt = datetime.strptime(str(min_time), '%Y-%m-%d %H:%M:%S')
                
                # 시간 차이 계산 (절댓값)
                time_diff = abs((tic_dt - min_dt).total_seconds())
                
                # 같은 분 내의 데이터를 우선적으로 찾기
                if tic_dt.replace(second=0, microsecond=0) == min_dt.replace(second=0, microsecond=0):
                    return i
                
                # 가장 가까운 시간의 분봉 데이터 찾기 (5분 이내)
                if time_diff < min_time_diff and time_diff <= 300:  # 5분 = 300초
                    min_time_diff = time_diff
                    best_match_idx = i
            
            if best_match_idx >= 0:
                min_dt = min_times[best_match_idx]
                return best_match_idx
           
            return -1  # 매칭되는 분봉 데이터 없음
        except Exception as ex:
            self.logger.error(f"분봉 데이터 매칭 실패: {ex}")
            return -1
    
    async def save_trade_record(self, code, datetime_str, order_type, quantity, price, strategy=""):
        """매매 기록 저장 (비동기 I/O)"""
        try:
            async with aiosqlite.connect(self.db_path, timeout=10.0) as conn:
                cursor = await conn.cursor()
                
                amount = quantity * price
                
                await cursor.execute('''
                    INSERT INTO trade_records 
                    (code, datetime, order_type, quantity, price, amount, strategy)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (code, datetime_str, order_type, quantity, price, amount, strategy))
                
                await conn.commit()
                
                self.logger.debug(f"매매 기록 저장: {code} {order_type} {quantity}주 @ {price}")
            
        except Exception as ex:
            self.logger.error(f"매매 기록 저장 실패: {ex}", exc_info=True)
    

# ==================== 키움 트레이더 클래스 ====================
class KiwoomTrader(QObject):
    """키움 REST API 기반 트레이더 클래스"""
    
    signal_log = pyqtSignal(str)
    signal_update_balance = pyqtSignal(dict)
    signal_order_result = pyqtSignal(str, str, int, float, bool)  # code, order_type, quantity, price, success
    
    def __init__(self, client, buycount, parent=None):
        super().__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client = client
        self.buycount = buycount
        self.parent = parent
        self.db_manager = AsyncDatabaseManager()
        # 비동기 데이터베이스 초기화는 별도로 호출
        self._init_database_async()
        
        # PyQt6에서는 QTextCursor 메타타입 등록이 불필요함
        
        # 포트폴리오 관리
        self.holdings = {}  # 보유 종목
        self.buy_prices = {}  # 매수 가격
        self.buy_times = {}  # 매수 시간
        self.highest_prices = {}  # 최고가 추적

        # 매도 주문 진행 중인 종목 추적 (중복 매도 방지)
        self.pending_sell_orders = set()

        # 주문 번호별 매도 정보 추적 (부분 매도 완료 알림용)
        self.sell_order_details = {}  # {order_no: {'code': str, 'total_qty': int, 'filled_qty': int}}

        # 매수 주문 진행 중인 종목 추적 (중복 매수 방지)
        self.pending_buy_orders = set()
        
        # 웹소켓 실시간 데이터 저장소
        self.balance_data = {}  # 웹소켓 실시간 잔고 데이터
        self.execution_data = {}  # 웹소켓 실시간 체결 데이터
        
        # 현금 조회 캐시 (API 호출 빈도 제한)
        self._cash_cache = 0.0
        self._cash_cache_time = 0
        # 예수금 조회 동시성 제어를 위한 Lock
        self._cash_query_lock = asyncio.Lock()

        # 종목별 매수 주문 동시성 제어를 위한 Lock
        self._buy_order_locks = {}
        
        # 설정 로드
        self.load_settings()

        self.logger.debug(f"키움 트레이더 초기화 완료 (목표 매수 종목 수: {self.buycount})")
    
    def _init_database_async(self):
        """비동기 데이터베이스 초기화 트리거"""
        try:
            # qasync 환경에서 메인 이벤트 루프 사용 시도
            try:
                loop = asyncio.get_running_loop()
                # 이미 실행 중인 이벤트 루프가 있으면 태스크로 실행
                loop.create_task(self.db_manager.init_database())
                self.logger.debug("✅ DB 초기화를 비동기 태스크로 시작")
                return
            except RuntimeError:
                # 실행 중인 이벤트 루프가 없음 - ThreadPoolExecutor로 처리
                self.logger.debug("⚠️ 실행 중인 이벤트 루프가 없어 ThreadPoolExecutor 사용")
                pass
            
            def run_async_init():
                try:
                    # 새로운 이벤트 루프 생성
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        # 비동기 데이터베이스 초기화 실행
                        return loop.run_until_complete(self.db_manager.init_database())
                    finally:
                        loop.close()
                except Exception as e:
                    self.logger.error(f"비동기 데이터베이스 초기화 실행 오류: {e}", exc_info=True)
                    return None
            
            # 별도 스레드에서 비동기 초기화 실행
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_async_init)
                future.result(timeout=30)  # 30초 타임아웃
                
        except Exception as ex:
            self.logger.error(f"비동기 데이터베이스 초기화 트리거 실패: {ex}", exc_info=True)
    
    def load_settings(self):
        """설정 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # 매매 설정
            self.evaluation_interval = config.getint('TRADING', 'evaluation_interval', fallback=5)

            # 수수료/세금 설정 (인라인 주석 처리)
            commission_rate_str = config.get('TRADING', 'commission_rate', fallback='0.00015')
            tax_rate_str = config.get('TRADING', 'tax_rate', fallback='0.0018')
            
            # ';' 문자로 주석을 분리하고 float으로 변환
            self.commission_rate = float(commission_rate_str.split(';')[0].strip())
            self.tax_rate = float(tax_rate_str.split(';')[0].strip())

            
            # 데이터 저장 설정
            self.data_saving_interval = config.getint('DATA_SAVING', 'interval_seconds', fallback=5)
            
            # 차트 업데이트 설정
            self.chartdata_update_interval = config.getint('CHART', 'chartdata_update_interval', fallback=10)
            
            self.logger.debug("설정 로드 완료")
            
        except Exception as ex:
            self.logger.error(f"설정 로드 실패: {ex}", exc_info=True)
    
    async def get_current_price(self, code):
        """현재가 조회 (실패 시 0 반환하여 fallback 처리) (비동기)"""
        try:
            price_data = await self.client.get_stock_current_price(code)
            return price_data.get('current_price', 0)
        except Exception as ex:
            self.logger.debug(f"현재가 조회 실패 ({code}) - fallback 처리됨", exc_info=True)
            return 0
    
    async def place_buy_order(self, code, quantity, price=0, strategy=""):
        """매수 주문 (키움 REST API 기반) (비동기)"""
        # 종목별 Lock 가져오기 또는 생성
        if code not in self._buy_order_locks:
            self._buy_order_locks[code] = asyncio.Lock()
        lock = self._buy_order_locks[code]

        async with lock:
            try:
                # Lock을 획득한 후, 다시 한번 pending_buy_orders를 확인하여
                # 다른 작업이 이미 주문을 시작했는지 최종적으로 체크합니다.
                if code in self.pending_buy_orders:
                    self.logger.debug(f"⏳ [{code}] 다른 작업이 이미 매수 주문을 진행 중이므로 현재 작업은 건너뜁니다.")
                    return False

                # 1. 보유 종목 확인 (이미 보유 중인 종목은 매수 제외)
                if self.parent and hasattr(self.parent, 'boughtBox'):
                    for i in range(self.parent.boughtBox.count()):
                        item_code = self.parent.boughtBox.item(i).text()
                        if item_code == code:
                            self.logger.info(f"⚠️ 매수 주문 취소: {code}는 이미 보유 중인 종목입니다.")
                            return False
                
                # 2. 최대 보유 종목 수 확인
                if self.parent and hasattr(self.parent, 'login_handler'):
                    max_count = self.parent.login_handler.get_target_buy_count()
                    current_count = self.parent.login_handler.get_current_holdings_count()
                    available_buy_count = self.parent.login_handler.get_available_buy_count()
                    
                    if available_buy_count <= 0:
                        self.logger.warning(f"⚠️ 매수 주문 취소: 최대 보유 종목 수 도달 ({code})")
                        self.logger.warning(f"   현황: 최대 {max_count}종목, 현재 {current_count}종목, 가능 {available_buy_count}종목")
                        return False
                    else:
                        # 매수 주문 진행 중 상태로 설정
                        self.pending_buy_orders.add(code)
                        self.logger.debug(f"⏳ [{code}] 매수 주문 진행 중 상태로 설정 (중복 주문 방지)")

                        self.logger.debug(f"✅ 매수 가능 확인: {code} (현재 {current_count}/{max_count}종목, 가능 {available_buy_count}종목)")
                
                # 키움 REST API를 통한 매수 주문
                success = await self.client.place_buy_order(code, quantity, price)
                
                if success:
                    # 매수 기록 저장 (비동기 태스크로 실행)
                    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    # 매도 주문 진행 중 상태에서 제거 (매수 후 다시 매도 가능하도록)
                    if code in self.pending_sell_orders:
                        self.pending_sell_orders.discard(code)
                    # qasync 환경에서 안전하게 태스크 생성
                    try:
                        asyncio.get_running_loop()  # 루프 확인
                        asyncio.create_task(self.db_manager.save_trade_record(code, current_time, "buy", quantity, price, strategy))
                    except RuntimeError:
                        self.logger.warning("⚠️ 이벤트 루프가 없어 매수 기록 저장을 건너뜁니다")
                    
                    # 포트폴리오 업데이트
                    self.buy_prices[code] = price if price > 0 else await self.get_current_price(code)
                    self.buy_times[code] = datetime.now()
                    self.highest_prices[code] = self.buy_prices[code]
                    
                    # holdings 딕셔너리 업데이트 (매도 평가를 위한 동기화)
                    self.holdings[code] = {
                        'quantity': quantity,
                        'average_price': self.buy_prices[code],
                        'current_price': self.buy_prices[code]
                    }
                    # 매수 전략 이름 저장 (섹션 이름만 저장하여 간결화)
                    buy_strategy_name = strategy
                    if buy_strategy_name.startswith('['):
                        try:
                            buy_strategy_name = buy_strategy_name.split(']')[0][1:]
                        except Exception: pass
                    self.holdings[code]['buy_strategy'] = buy_strategy_name
                    
                    self.logger.debug(f"✅ holdings 업데이트: {code} (수량: {quantity}주, 평단: {self.buy_prices[code]:,}원)")
                    
                    # 보유종목 리스트에 즉시 추가 (종목 수 제한 동기화)
                    if self.parent and hasattr(self.parent, 'boughtBox'):
                        # 이미 리스트에 있는지 확인
                        # 모니터링 리스트에도 추가 (중요)
                        stock_name = f"종목{code}" # API 호출 없이 기본 이름 사용
                        self.parent.monitoring_manager.add_stock_to_monitoring(code, stock_name)

                        already_in_list = False
                        for i in range(self.parent.boughtBox.count()):
                            if self.parent.boughtBox.item(i).text() == code:
                                already_in_list = True
                                break
                        
                        if not already_in_list:
                            self.parent.boughtBox.addItem(code)
                            new_count = self.parent.boughtBox.count()
                            self.logger.debug(f"✅ 보유종목 리스트에 추가: {code} (총 {new_count}개 종목 보유)")
                    
                    self.signal_order_result.emit(code, "buy", quantity, price, True)
                    self.logger.debug(f"✅ 매수 주문 성공: {code} {quantity}주 (키움 REST API)")
                    return True
                else:
                    self.signal_order_result.emit(code, "buy", quantity, price, False)
                    # 주문 실패 시, '매수 주문 진행 중' 상태 해제
                    if code in self.pending_buy_orders:
                        self.pending_buy_orders.discard(code)
                        self.logger.info(f"🟢 [{code}] 매수 주문 실패로 진행 중 상태 해제")
                    self.logger.error(f"❌ 매수 주문 실패: {code}")
                    return False
                    
            except Exception as ex:
                self.logger.error(f"매수 주문 중 오류 ({code}): {ex}", exc_info=True)
                if code in self.pending_buy_orders:
                    self.pending_buy_orders.discard(code)
                    logging.info(f"🟢 [{code}] 매수 주문 오류로 진행 중 상태 해제")
                self.signal_order_result.emit(code, "buy", quantity, price, False)
                return False
    
    async def place_sell_order(self, code, quantity, price=0, strategy=""):
        """매도 주문 (키움 REST API 기반) (비동기)"""
        try:
            # quantity가 0 이하인 경우 주문을 실행하지 않음
            if quantity <= 0: # type: ignore
                self.logger.warning(f"⚠️ 매도 주문 수량이 0 이하이므로 주문을 실행하지 않습니다: {code}, 수량: {quantity}")
                return False

            # 키움 REST API를 통한 매도 주문
            # 주문 전, '주문 진행 중' 상태로 설정
            self.pending_sell_orders.add(code)
            self.logger.debug(f"⏳ [{code}] 매도 주문 진행 중 상태로 설정 (중복 주문 방지)")

            success = await self.client.place_sell_order(code, quantity, price)
            
            if success:
                # 매도 기록 저장 (비동기 태스크로 실행)
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                sell_price = price if price > 0 else await self.get_current_price(code)
                # qasync 환경에서 안전하게 태스크 생성
                try:
                    asyncio.get_running_loop()  # 루프 확인
                    asyncio.create_task(self.db_manager.save_trade_record(code, current_time, "sell", quantity, sell_price, strategy))
                except RuntimeError:
                    self.logger.warning("⚠️ 이벤트 루프가 없어 매도 기록 저장을 건너뜁니다")
                
                # 보유 수량 확인 및 전량 매도 판단
                is_full_sell = False
                remaining_qty = 0
                
                # 1차: self.holdings에서 확인
                if code in self.holdings:
                    remaining_qty = self.holdings[code].get('quantity', 0)
                    is_full_sell = (remaining_qty <= quantity)
                else:
                    # 2차: 웹소켓 잔고 데이터에서 확인
                    balance_data = self.get_balance_data()
                    if code in balance_data.get('holdings', {}):
                        remaining_qty = balance_data['holdings'][code].get('quantity', 0)
                        is_full_sell = (remaining_qty <= quantity)
                    else:
                        # 보유 수량 정보 없음 → 전량 매도로 간주
                        is_full_sell = True
                        self.logger.debug(f"⚠️ {code} 보유 수량 정보 없음 - 전량 매도로 처리")
                
                # 전량 매도 시 최고가 정보 초기화
                if is_full_sell and code in self.highest_prices:
                            del self.highest_prices[code]
                            self.logger.debug(f"🗑️ {code} 최고가 정보 초기화 (전량 매도)")

                # 부분 매도 주문 정보 추적
                ord_no = self.client.last_order_no
                if ord_no:
                    self.sell_order_details[ord_no] = {
                        'code': code,
                        'total_qty': quantity,
                        'filled_qty': 0
                    }
                    self.logger.debug(f"📋 부분 매도 주문 추적 시작: 주문번호={ord_no}, 종목={code}, 수량={quantity}주")
                
                # holdings 딕셔너리 업데이트 (매도 평가를 위한 동기화)
                if is_full_sell:
                    # 전량 매도 시 holdings에서 제거
                    if code in self.holdings:
                        del self.holdings[code]
                        self.logger.debug(f"✅ holdings에서 제거: {code} (전량 매도)")
                    # buy_prices, buy_times도 함께 정리
                    if code in self.buy_prices:
                        del self.buy_prices[code]
                    if code in self.buy_times:
                        del self.buy_times[code]
                else:
                    # 부분 매도 시 수량만 업데이트
                    if code in self.holdings:
                        new_quantity = remaining_qty - quantity
                        self.holdings[code]['quantity'] = new_quantity
                        self.logger.debug(f"✅ holdings 수량 업데이트: {code} ({remaining_qty}주 → {new_quantity}주)")
                
                # 전량 매도 시 보유종목 리스트에서 즉시 제거 (종목 수 제한 동기화)
                if is_full_sell and self.parent and hasattr(self.parent, 'boughtBox'):
                    for i in range(self.parent.boughtBox.count()):
                        if self.parent.boughtBox.item(i).text() == code:
                            self.parent.boughtBox.takeItem(i)
                            new_count = self.parent.boughtBox.count()
                            self.logger.info(f"✅ 보유종목 리스트에서 제거: {code} (전량 매도, 남은 종목 {new_count}개)")
                            break
                elif not is_full_sell:
                    self.logger.debug(f"ℹ️ {code} 부분 매도 (보유: {remaining_qty}주, 매도: {quantity}주)")
                
                self.signal_order_result.emit(code, "sell", quantity, price, True)
                self.logger.debug(f"✅ 매도 주문 성공: {code} {quantity}주 (키움 REST API)")
                return True
            else:
                self.signal_order_result.emit(code, "sell", quantity, price, False)
                # 주문 실패 시, '주문 진행 중' 상태 해제
                if code in self.pending_sell_orders:
                    self.pending_sell_orders.discard(code)
                    self.logger.info(f"🟢 [{code}] 매도 주문 실패로 진행 중 상태 해제")
                self.logger.error(f"❌ 매도 주문 실패: {code}")
                return False
                
        except Exception as ex:
            self.logger.error(f"매도 주문 중 오류 ({code}): {ex}", exc_info=True)
            self.signal_order_result.emit(code, "sell", quantity, price, False)
            return False
        finally:
            # finally 블록은 주문 성공/실패와 관계없이 실행되므로, 실패 시에만 상태를 해제하도록 위로 이동
            pass
    
    def get_portfolio_status(self):
        """포트폴리오 상태 조회 (웹소켓 balance_data와 동기화)"""
        try:
            # self.holdings, self.buy_prices, self.buy_times를 웹소켓 잔고와 동기화
            self._sync_holdings_with_websocket()

            merged_holdings = self.holdings.copy()
            merged_buy_prices = self.buy_prices.copy()
            merged_buy_times = self.buy_times.copy()
            
            # 웹소켓 balance_data에서 보유 종목 보완 (self.holdings에 없지만 웹소켓에는 있는 경우)
            try:
                if (hasattr(self, 'parent') and self.parent and 
                    hasattr(self.parent, 'login_handler') and self.parent.login_handler and
                    hasattr(self.parent.login_handler, 'websocket_client') and self.parent.login_handler.websocket_client and
                    hasattr(self.parent.login_handler.websocket_client, 'balance_data')):
                    
                    ws_balance_data = self.parent.login_handler.websocket_client.balance_data
                    if ws_balance_data:
                        # 웹소켓 balance_data에 없는 종목은 self.holdings에서도 제거 (전량 매도 완료)
                        codes_to_remove = []
                        for code in merged_holdings.keys():
                            if code not in ws_balance_data:
                                codes_to_remove.append(code)
                        
                        for code in codes_to_remove:
                            del merged_holdings[code]
                            if code in merged_buy_prices:
                                del merged_buy_prices[code]
                            if code in merged_buy_times:
                                del merged_buy_times[code]
                            self.logger.debug(f"🗑️ [{code}] get_portfolio_status에서 제거 (웹소켓 balance_data에 없음)")
                        
                        # 웹소켓 balance_data의 종목들을 처리
                        for code, balance_info in ws_balance_data.items():
                            quantity = balance_info.get('quantity', 0)
                            order_available_qty = balance_info.get('order_available_qty', 0)
                            
                            # 수량이 0인 경우 holdings에서 제거 (전량 매도 완료)
                            if quantity == 0:
                                if code in merged_holdings:
                                    del merged_holdings[code]
                                    self.logger.debug(f"🗑️ [{code}] get_portfolio_status에서 제거 (웹소켓 수량=0)")
                                if code in merged_buy_prices:
                                    del merged_buy_prices[code]
                                if code in merged_buy_times:
                                    del merged_buy_times[code]
                            elif quantity > 0:
                                # 웹소켓에 있지만 self.holdings에 없는 경우 추가
                                if code not in merged_holdings:
                                    merged_holdings[code] = {'quantity': quantity}
                                    # 매입단가와 시간이 없으면 웹소켓 데이터 활용
                                    if code not in merged_buy_prices:
                                        merged_buy_prices[code] = balance_info.get('average_price', 0)
                                    if code not in merged_buy_times:
                                        # 웹소켓에는 시간 정보가 없으므로 현재 시간 사용
                                        merged_buy_times[code] = datetime.now()
                                else:
                                    # self.holdings에도 있지만 수량이 다를 수 있음 (웹소켓이 더 정확할 수 있음)
                                    ws_quantity = quantity
                                    holdings_quantity = merged_holdings[code].get('quantity', 0)

                                    # 부분 매도 후 buy_price가 유지되도록 보장
                                    if code not in merged_buy_prices and code in self.buy_prices:
                                        merged_buy_prices[code] = self.buy_prices[code]
                                        self.logger.debug(f"🔄 [{code}] 부분 매도 후 buy_price 복원: {self.buy_prices[code]:,.0f}원")
                                    if code not in merged_buy_times and code in self.buy_times:
                                        merged_buy_times[code] = self.buy_times[code]
                                        self.logger.debug(f"🔄 [{code}] 부분 매도 후 buy_time 복원")

                                    if ws_quantity != holdings_quantity: # type: ignore
                                        # 웹소켓 수량으로 업데이트
                                        merged_holdings[code]['quantity'] = ws_quantity
                                        # 매입단가도 업데이트 (없거나 0인 경우)
                                        if code not in merged_buy_prices or merged_buy_prices[code] == 0:
                                            merged_buy_prices[code] = balance_info.get('average_price', 0)
            except Exception as ws_ex:
                # 웹소켓 동기화 실패해도 계속 진행 (경고만 출력)
                self.logger.debug(f"웹소켓 balance_data 동기화 중 오류 (무시): {ws_ex}", exc_info=True)
            
            portfolio = {
                'holdings': merged_holdings,
                'buy_prices': merged_buy_prices,
                'buy_times': merged_buy_times,
                'highest_prices': self.highest_prices.copy(),
                'total_holdings': len(merged_holdings),
                'max_holdings': self.buycount
            }
            return portfolio
        except Exception as ex:
            self.logger.error(f"포트폴리오 상태 조회 실패: {ex}", exc_info=True)
            return {}

    def _sync_holdings_with_websocket(self):
        """self.holdings를 웹소켓 잔고 데이터와 동기화"""
        try:
            if not (hasattr(self, 'parent') and self.parent and
                    hasattr(self.parent, 'login_handler') and self.parent.login_handler and
                    hasattr(self.parent.login_handler, 'websocket_client') and self.parent.login_handler.websocket_client and
                    hasattr(self.parent.login_handler.websocket_client, 'balance_data')):
                return

            ws_balance_data = self.parent.login_handler.websocket_client.balance_data
            if not ws_balance_data:
                # 웹소켓 데이터가 비어있으면 self.holdings도 비워야 함 (전량 매도된 경우)
                if self.holdings:
                    self.logger.debug("🗑️ 웹소켓 잔고가 비어있어 self.holdings를 초기화합니다.")
                    self.holdings.clear()
                    self.buy_prices.clear()
                    self.buy_times.clear()
                    self.highest_prices.clear()
                return

            # 웹소켓에 없는 종목은 self.holdings에서 제거
            codes_in_holdings = set(self.holdings.keys())
            codes_in_websocket = set(ws_balance_data.keys())
            
            for code in codes_in_holdings - codes_in_websocket:
                del self.holdings[code]
                if code in self.buy_prices: del self.buy_prices[code]
                if code in self.buy_times: del self.buy_times[code]
                if code in self.highest_prices: del self.highest_prices[code]
                self.logger.debug(f"🗑️ [{code}] holdings 동기화: 웹소켓에 없어 제거됨")

            # 웹소켓에 있는 종목은 self.holdings에 추가/업데이트
            for code, balance_info in ws_balance_data.items():
                quantity = balance_info.get('quantity', 0)
                if quantity > 0:
                    if code not in self.holdings:
                        self.holdings[code] = {'quantity': quantity}
                        self.buy_prices[code] = balance_info.get('average_price', 0)
                        self.buy_times[code] = datetime.now() # 시간 정보가 없으므로 현재 시간으로 설정
                        self.logger.debug(f"🆕 [{code}] holdings 동기화: 웹소켓 잔고로 신규 추가")
                    else:
                        self.holdings[code]['quantity'] = quantity # 수량 동기화
                else:
                    # 수량이 0인 경우 (전량 매도 완료) holdings에서 제거
                    if code in self.holdings:
                        del self.holdings[code]
                        self.logger.debug(f"🗑️ [{code}] holdings 동기화: 웹소켓 수량=0으로 제거됨")
                    if code in self.buy_prices:
                        del self.buy_prices[code]
                    if code in self.buy_times:
                        del self.buy_times[code]
                    if code in self.highest_prices:
                        del self.highest_prices[code]
        except Exception as ex:
            self.logger.warning(f"self.holdings와 웹소켓 잔고 동기화 실패: {ex}", exc_info=True)
    def get_balance_data(self):
        """웹소켓 실시간 잔고 데이터 조회
        주의: 이 메서드는 웹소켓을 통한 실시간 잔고 데이터를 반환합니다.
        REST API 계좌평가현황과는 별개의 데이터입니다.
        """
        if not hasattr(self, 'balance_data'):
            self.balance_data = {}
        
        # 기본 구조가 없으면 기본값 반환
        if not self.balance_data:
            return {
                'available_cash': 0,
                'holdings': {},
                'total_assets': 0
            }
        
        return self.balance_data.copy()

    async def get_account_balance(self) -> Dict:
        """투자계좌자산현황조회 - 투자가능 현금 조회용 (비동기)
        매수 시 투자가능 현금을 확인하기 위한 API
        """
        try:
            if not await self.client.check_token_validity():
                return {}
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.client.mock_url if self.client.is_mock else self.client.base_url
            url = f"{server_url}/uapi/domestic-stock/v1/trading/inquire-account-balance"
            
            # 헤더 설정
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.client.access_token}',
                'appkey': self.client.app_key,
                'appsecret': self.client.app_secret,
                'tr_id': 'CTRP6548R',  # 투자계좌자산현황조회
            }
            
            # 요청 데이터
            params = {
                'CANO': self.client.account_number,  # 종합계좌번호
                'ACNT_PRDT_CD': self.client.account_product_code,  # 계좌상품코드
                'INQR_DVSN_1': '',  # 조회구분1
                'BSPR_BF_DT_APLY_YN': ''  # 기준가이전일자적용여부
            }
            
            # POST 요청 (비동기)
            await self.client._ensure_client()
            response = await self.client.post(url, headers=headers, json=params, timeout=10.0)
            
            if response.status_code == 200:
                data = response.json()
                
                # 응답 코드 확인
                if data.get('rt_cd') == '0':
                    self.logger.debug("투자계좌자산현황조회 성공")
                    return data
                else:
                    return_msg = data.get('msg1', '알 수 없는 오류')
                    self.logger.error(f"투자계좌자산현황조회 실패: {return_msg}", exc_info=True)
                    return {}
            else:
                self.logger.error(f"투자계좌자산현황조회 실패: {response.status_code}", exc_info=True)
                return {}
                
        except Exception as e:
            self.logger.error(f"투자계좌자산현황조회 중 오류: {e}", exc_info=True)
            return {}

    async def get_available_cash(self) -> float:
        """투자가능 현금 조회 (비동기)
        매수 시 사용할 수 있는 현금 금액을 반환
        (캐싱을 통해 API 호출 빈도를 제한하여 429 오류 방지)
        """
        async with self._cash_query_lock:
            try:
                # 캐시 유효성 확인 (5초 이내면 캐시 사용)
                current_time = time.time()
                cache_validity_period = 5  # 5초
                
                if hasattr(self, '_cash_cache_time') and (current_time - self._cash_cache_time) < cache_validity_period:
                    return self._cash_cache
                
                # API 제한 확인 및 대기
                await ApiLimitManager.check_api_limit_and_wait_async("예수금상세현황 조회", request_type="deposit")
                
                deposit_data = await self.client.get_deposit_detail()
                if not deposit_data:
                    return self._cash_cache  # 캐시된 값 반환
                
                # 주문가능금액 조회 (투자가능 현금)
                available_cash = float(deposit_data.get('ord_alow_amt', 0))
                
                # 캐시 업데이트
                self._cash_cache = available_cash
                self._cash_cache_time = current_time
                
                self.logger.debug(f"투자가능 현금: {available_cash:,.0f}원 (캐시: {cache_validity_period}초)")
                return available_cash
            
            except Exception as e:
                self.logger.error(f"투자가능 현금 조회 중 오류: {e}", exc_info=True)
                return self._cash_cache if hasattr(self, '_cash_cache') else 0.0

# ==================== 키움 전략 클래스 ====================
class KiwoomStrategy(QObject):
    """키움 REST API 기반 전략 클래스"""
    
    # 시그널 정의
    signal_strategy_result = pyqtSignal(str, str, dict)  # code, action, data
    clear_signal = pyqtSignal()
    
    def __init__(self, trader, parent):
        super().__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.trader = trader
        self.client = trader.client
        self.db_manager = trader.db_manager
        self.parent = parent

        # 종목별 매수 신호 생성 동시성 제어를 위한 Lock
        self._buy_signal_locks: Dict[str, asyncio.Lock] = {}
        
        # PyQt6에서는 QTextCursor 메타타입 등록이 불필요함
        
        # 전략 설정 로드
        self.load_strategy_config()
            
    def load_strategy_config(self):
        """전략 설정 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # 현재 전략 로드
            self.current_strategy = config.get('SETTINGS', 'last_strategy', fallback='통합 전략')
            
            # 전략별 설정 로드 - [STRATEGIES] 섹션 기반으로 동적 로드
            self.strategy_config = {}
            if config.has_section('STRATEGIES'):
                for key, strategy_name in config.items('STRATEGIES'):
                    if key.startswith('stg_') or key == 'stg_integrated':
                        # 해당 전략명과 일치하는 섹션이 있으면 로드
                        if config.has_section(strategy_name):
                            self.strategy_config[strategy_name] = dict(config.items(strategy_name)) # type: ignore
                            self.logger.debug(f"✅ 전략 설정 로드: {strategy_name}")
            
            self.logger.debug(f"전략 설정 로드 완료: {self.current_strategy}")
            
        except Exception as ex:
            self.logger.error(f"전략 설정 로드 실패: {ex}", exc_info=True)
    
    async def evaluate_strategy(self, code, market_data):
        """전략 평가 및 실행 (비동기)"""
        try:
            # 디버그 로그: 최초 1회만 출력 (종목별)
            if not hasattr(self, '_eval_debug_codes'):
                self._eval_debug_codes = set()
            
            is_first_eval = code not in self._eval_debug_codes
            if is_first_eval:
                self.logger.debug(f"📊 [{code}] 전략 평가 시작 (전략: {self.current_strategy})")
                self._eval_debug_codes.add(code)
            
            # 현재 전략에 따른 매수/매도 신호 평가
            # 1. 통합 전략이 선택된 경우: 모든 종목에 통합 전략 적용
            # 2. 조건검색으로 찾은 종목: 해당 조건검색의 전략 사용
            # 3. 그 외: 현재 선택된 전략 사용
            
            stock_condition_map = self.parent.stock_condition_map if hasattr(self.parent, 'stock_condition_map') else {}
            
            # UI에서 선택된 전략
            selected_strategy_name = self.current_strategy
            
            # 실제 적용할 전략 결정
            # 1순위: 조건검색으로 포착된 종목은 해당 조건검색식의 전략을 사용
            if code in stock_condition_map:
                effective_strategy_name = stock_condition_map[code]
                if is_first_eval:
                    self.logger.debug(f"📍 [{code}] 조건검색 전략 사용: {effective_strategy_name} (UI 선택: {selected_strategy_name})")
            # 2순위: 그 외의 경우(수동 추가 등)는 UI에서 선택된 전략을 사용
            else:
                effective_strategy_name = selected_strategy_name
                if is_first_eval:
                    self.logger.debug(f"📍 [{code}] UI 선택 전략 사용: {effective_strategy_name}")
            
            if effective_strategy_name != "통합 전략" and effective_strategy_name not in self.strategy_config:
                if is_first_eval:
                    self.logger.warning(f"⚠️ [{code}] 전략 '{effective_strategy_name}'이 설정에 없음")
                return
            
            if is_first_eval:
                self.logger.debug(f"✅ [{code}] 전략 설정 확인됨: {effective_strategy_name}")
            
            # 매수 신호 평가
            buy_signals = await self.get_buy_signals(code, market_data, effective_strategy_name)
            if buy_signals:
                self.logger.debug(f"📈 [{code}] 매수 신호 {len(buy_signals)}개 발견")
                await self.execute_buy_signals(code, buy_signals)
            elif is_first_eval:
                self.logger.debug(f"ℹ️ [{code}] 매수 조건 미충족")
            
            # 매도 신호 평가 (보유 종목인 경우에만)
            portfolio = self.trader.get_portfolio_status()
            if code in portfolio['holdings']:
                if is_first_eval:
                    self.logger.debug(f"🔎 [{code}] 보유 종목 - 매도 평가 진행")
                sell_signals = await self.get_sell_signals(code, market_data, effective_strategy_name)
                if sell_signals:
                    self.logger.debug(f"📉 [{code}] 매도 신호 {len(sell_signals)}개 발견")
                    await self.execute_sell_signals(code, sell_signals)
            else:
                # 보유 종목이 아닌 경우 디버그 로그 (최초 1회만)
                if is_first_eval:
                    # 웹소켓 balance_data 확인
                    ws_has_stock = False
                    try:
                        if (hasattr(self.parent, 'login_handler') and self.parent.login_handler and
                            hasattr(self.parent.login_handler, 'websocket_client') and self.parent.login_handler.websocket_client and
                            hasattr(self.parent.login_handler.websocket_client, 'balance_data')):
                            ws_balance_data = self.parent.login_handler.websocket_client.balance_data
                            if ws_balance_data and code in ws_balance_data:
                                ws_quantity = ws_balance_data[code].get('quantity', 0)
                                if ws_quantity > 0:
                                    ws_has_stock = True
                                    self.logger.warning(f"⚠️ [{code}] 웹소켓에는 보유 중이지만 self.holdings에 없음 (웹소켓 수량: {ws_quantity}주)")
                                    self.logger.warning(f"get_portfolio_status 동기화 필요 - holdings: {list(portfolio['holdings'].keys())}, 웹소켓: {list(ws_balance_data.keys())}")
                    except Exception as ws_check_ex:
                        self.logger.debug(f"웹소켓 체크 중 오류: {ws_check_ex}", exc_info=True)
                    
                    if not ws_has_stock:
                        self.logger.debug(f"ℹ️ [{code}] 보유 종목 아님 - 매도 평가 건너뜀")
                    
        except Exception as ex:
            self.logger.error(f"전략 평가 실패 ({code}): {ex}", exc_info=True)
            
    
    async def get_buy_signals(self, code, market_data, strategy_name):
        """매수 신호 생성 - strategy_utils를 사용한 기술적 지표 기반 평가"""
        try:
            # 종목별 Lock 가져오기 또는 생성
            if code not in self._buy_signal_locks:
                self._buy_signal_locks[code] = asyncio.Lock()
            lock = self._buy_signal_locks[code]

            async with lock:
                signals = []
                
                # 디버그 로그: 최초 1회만 출력 (종목별)
                if not hasattr(self, '_buy_signal_debug_codes'):
                    self._buy_signal_debug_codes = set()
                
                is_first_check = code not in self._buy_signal_debug_codes
                if is_first_check:
                    self.logger.debug(f"🔍 [{code}] 매수 신호 검사 시작")
                    self._buy_signal_debug_codes.add(code)
                
                # 포트폴리오 상태 확인
                portfolio = self.trader.get_portfolio_status()
                if portfolio['total_holdings'] >= portfolio['max_holdings']:
                    if is_first_check:
                        self.logger.debug(f"⚠️ [{code}] 매수 불가: 보유 종목 수 한도 도달 ({portfolio['total_holdings']}/{portfolio['max_holdings']})")
                    return signals
                
                # 이미 보유 중인 종목인지 확인
                if code in portfolio['holdings']:
                    if is_first_check:
                        self.logger.debug(f"⚠️ [{code}] 매수 불가: 이미 보유 중")
                    return signals

                # '매수 주문 진행 중'인 종목은 매수 신호 생성 건너뛰기 (중복 주문 방지 강화)
                if hasattr(self.trader, 'pending_buy_orders') and code in self.trader.pending_buy_orders:
                    if is_first_check:
                        self.logger.debug(f"⏳ [{code}] 매수 주문이 이미 진행 중이므로 신호 생성을 건너뜁니다.")
                    return signals

                # '매수 주문 진행 중'인 종목은 매수 신호 생성 건너뛰기
                if code in self.trader.pending_buy_orders:
                    if is_first_check:
                        self.logger.debug(f"⏳ [{code}] 매수 주문이 이미 진행 중이므로 신호 생성을 건너뜁니다.")
                    return signals
                
                # 차트 데이터 가져오기 (틱/분봉) - chart_cache에서 직접 가져오기
                tic_chart_data = pd.DataFrame()
                min_chart_data = pd.DataFrame()
                previous_close = 0
                current_open = 0
                if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                    cache_data = self.parent.chart_cache.get_cached_data(code)
                    if cache_data:
                        tic_data = cache_data.get('tic_data', {})
                        min_data = cache_data.get('min_data', {})
                        previous_close = cache_data.get('previous_close', 0)
                        current_open = cache_data.get('current_open', 0)

                        if tic_data and len(tic_data.get('close', [])) > 0:
                            try:
                                tic_chart_data = pd.DataFrame(tic_data).dropna().reset_index(drop=True)
                            except Exception as ex:
                                if is_first_check:
                                    self.logger.warning(f"차트 데이터 변환 실패 ({code}): {ex}", exc_info=True)
                                tic_chart_data = pd.DataFrame()

                        if min_data and len(min_data.get('close', [])) > 0:
                            try:
                                min_chart_data = pd.DataFrame(min_data).dropna().reset_index(drop=True)
                            except Exception as ex:
                                if is_first_check:
                                    self.logger.warning(f"분봉 차트 데이터 변환 실패 ({code}): {ex}", exc_info=True)
                                min_chart_data = pd.DataFrame()

                        if is_first_check:
                            self.logger.debug(f"✅ [{code}] 차트 데이터 준비 완료: {len(tic_chart_data)}개 틱, {len(min_chart_data)}개 분봉")
                    else:
                        if is_first_check:
                            self.logger.warning(f"⚠️ [{code}] cache_data가 없음")
                else:
                    if is_first_check:
                        self.logger.warning(f"⚠️ [{code}] chart_cache 없음")
                
                # 매수 전략 로드
                # 차트 데이터가 비어있으면 평가를 건너뜀
                if tic_chart_data.empty:
                    if is_first_check:
                        self.logger.debug(f"ℹ️ [{code}] 틱 차트 데이터가 비어있어 매수 평가를 건너뜁니다.")
                    return signals

                buy_strategies = []

                # 통합 전략: '급등주' + '갭상승' 전략을 합산 적용
                if strategy_name == "통합 전략":
                    merged_sections = []
                    if '급등주' in self.strategy_config:
                        merged_sections.append(('급등주', self.strategy_config['급등주']))
                    if '갭상승' in self.strategy_config:
                        merged_sections.append(('갭상승', self.strategy_config['갭상승']))

                    for section_name, section_conf in merged_sections:
                        # buy_stg_* 만 숫자순 정렬해 결합
                        items = sorted([item for item in section_conf.items() if item[0].startswith('buy_stg_')], key=lambda x: int(x[0].split('_')[-1]))
                        items.sort(key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 999)
                        for key, value in items:
                            try:
                                strategy_data = json.loads(value)
                                # 전략명에 섹션 표기 추가(로그 가독성)
                                if isinstance(strategy_data, dict) and 'name' in strategy_data:
                                    strategy_data['name'] = f"[{section_name}] {strategy_data['name']}"
                                buy_strategies.append(strategy_data)
                            except json.JSONDecodeError:
                                if is_first_check: # type: ignore
                                    self.logger.warning(f"⚠️ [{code}] 매수 전략 파싱 실패: {section_name}.{key}")

                    if buy_strategies and is_first_check:
                        self.logger.debug(f"✅ [{code}] 통합 전략 로드 완료: 급등주+갭상승 매수 전략 {len(buy_strategies)}개")
                else:
                    # 개별 전략 섹션에서 매수 조건 가져오기
                    if strategy_name in self.strategy_config:
                        strategy_conf = self.strategy_config[strategy_name]
                        items = sorted([item for item in strategy_conf.items() if item[0].startswith('buy_stg_')], key=lambda x: int(x[0].split('_')[-1]))
                        items.sort(key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 999)
                        for key, value in items:
                            try:
                                strategy_data = json.loads(value)
                                buy_strategies.append(strategy_data)
                            except json.JSONDecodeError:
                                if is_first_check: # type: ignore
                                    self.logger.warning(f"⚠️ [{code}] 매수 전략 파싱 실패: {key}")
                        if buy_strategies and is_first_check:
                            self.logger.debug(f"✅ [{code}] strategy_config에서 매수 전략 {len(buy_strategies)}개 로드됨: {strategy_name}")
                
                # 전략이 없으면 기본 전략 사용 (매우 보수적)
                if not buy_strategies:
                    if is_first_check:
                        self.logger.warning(f"⚠️ [{code}] 매수 전략 없음 - 기본 전략 사용 (RSI < 30 + MACD 골든크로스)")
                    # 기본 전략: RSI 과매도 + MACD 골든크로스
                    buy_strategies = [{
                        'name': '기본 전략',
                        'conditions': [
                            {'indicator': 'RSI', 'operator': '<', 'value': 30},
                            {'indicator': 'MACD_HIST', 'operator': '>', 'value': 0}
                        ]
                    }]
                
                if is_first_check:
                    self.logger.debug(f"✅ [{code}] 최종 매수 전략 {len(buy_strategies)}개 준비 완료")
                
                # strategy_utils를 사용하여 매수 전략 평가
                safe_locals = strategy_utils.prepare_buy_strategy_locals(
                    code, tic_chart_data, min_chart_data, previous_close, current_open, portfolio
                )
                condition_met, matched_strategy = strategy_utils.evaluate_strategies(
                    buy_strategies, safe_locals, code, "매수"
                )
                
                if is_first_check:
                    self.logger.debug(f"ℹ️ [{code}] 매수 조건 {'충족' if condition_met else '미충족'}: {matched_strategy.get('name', 'N/A') if matched_strategy else ''}")
                
                if condition_met and matched_strategy:
                    current_price = market_data.get('current_price', 0)
                    
                    # 매수 수량 계산 (최대투자종목수 기반 분산투자)
                    # 주의: get_buy_signals는 동기 메서드이므로 비동기 호출이 어려움
                    available_cash = await self.trader.get_available_cash()
                    
                    # 가용자금이 0 이하이거나 현재가가 0이면 매수 신호 생성 안함
                    if available_cash <= 0:
                        if is_first_check:
                            self.logger.debug(f"ℹ️ [{code}] 가용자금 부족으로 매수 불가 ({available_cash:,.0f}원)")
                        return []
                    
                    if current_price <= 0:
                        if is_first_check:
                            self.logger.debug(f"ℹ️ [{code}] 현재가 정보 없음 - 매수 불가")
                        return []
                    
                    # 매수가능 종목수 조회
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'login_handler'):
                        available_buy_count = self.parent.login_handler.get_available_buy_count()
                    else:
                        available_buy_count = portfolio.get('max_holdings', 3) - portfolio.get('total_holdings', 0)
                    
                    # 매수 가능 종목수가 0 이하면 매수 신호 생성 안함
                    if available_buy_count <= 0:
                        if is_first_check:
                            self.logger.debug(f"ℹ️ [{code}] 최대 보유 종목수 도달 - 매수 불가")
                        return []
                    
                    # 한 종목당 투자 예산 = 가용자금 ÷ 매수가능종목수
                    budget = available_cash // available_buy_count
                    quantity = max(1, int(budget / current_price))
                    
                    strategy_display_name = matched_strategy.get('name', strategy_name)
                    self.logger.info(f"📈 매수 신호 발생: {code} - {strategy_display_name}")
                    self.logger.debug(f"💰 매수 수량 계산: 가용자금={available_cash:,.0f}원, 매수가능종목={available_buy_count}개")
                    self.logger.debug(f"   종목당예산={budget:,.0f}원, 현재가={current_price:,}원 → {quantity}주")
                    
                    signals.append({
                        'strategy': matched_strategy.get('name', strategy_name),
                        'code': code,
                        'quantity': quantity,
                        'price': 0,  # 시장가
                        'reason': f"기술적 지표 기반 매수 조건 충족: {matched_strategy.get('name', '')}"
                    })
                
                return signals
            
        except Exception as ex:
            self.logger.error(f"매수 신호 생성 실패 ({code}): {ex}", exc_info=True)
            traceback.print_exc()
            return []
    
    async def get_sell_signals(self, code, market_data, strategy_name):
        """매도 신호 생성 - strategy_utils를 사용한 기술적 지표 기반 평가 (비동기)"""
        try:
            signals = []
            
            # 디버그: 최초 1회만 매도 평가 시작 로그
            if not hasattr(self, '_sell_eval_codes'):
                self._sell_eval_codes = set()
            is_first_sell_check = code not in self._sell_eval_codes
            if is_first_sell_check:
                self._sell_eval_codes.add(code)
            
            # 보유 중인 종목인지 확인
            portfolio = self.trader.get_portfolio_status()
            if code not in portfolio['holdings']:
                # 매도 불가 로그 제거 (너무 빈번함)
                return signals

            # '주문 진행 중'인 종목은 매도 신호 생성 건너뛰기 (중복 주문 방지 강화)
            if code in self.trader.pending_sell_orders:
                if is_first_sell_check: # type: ignore
                    self.logger.debug(f"⏳ [{code}] 매도 주문이 이미 진행 중이므로 신호 생성을 건너뜁니다.")
                return signals

            # '주문 진행 중'인 종목은 매도 신호 생성 건너뛰기
            if code in self.trader.pending_sell_orders:
                if is_first_sell_check: # type: ignore
                    self.logger.debug(f"⏳ [{code}] 매도 주문이 이미 진행 중이므로 신호 생성을 건너뜁니다.")
                return signals
            
            # 최초 매도 평가 시작 로그
            if is_first_sell_check: # type: ignore
                self.logger.debug(f"🔍 [{code}] 매도 평가 시작 (전략: {strategy_name})")
            
            # 보유 정보
            holding_info = portfolio['holdings'][code]
            
            # 매수 시 사용된 전략 확인
            buy_strategy_name = holding_info.get('buy_strategy', strategy_name)
            # 매수 전략 이름에서 섹션 이름 추출 (예: "[급등주] 2순위..." -> "급등주")
            if buy_strategy_name and buy_strategy_name.startswith('['):
                try:
                    strategy_name = buy_strategy_name.split(']')[0][1:]
                except Exception: pass
            
            # 보유 정보
            holding_info = portfolio['holdings'][code]
            buy_price = portfolio['buy_prices'].get(code, 0)
            buy_time = portfolio['buy_times'].get(code)
            quantity = holding_info.get('quantity', 0)
            
            if buy_price <= 0 or quantity <= 0:
                # 보유 정보 불완전 로그 제거 (너무 빈번함)
                return signals

            # 매수 후 최소 보유 시간(Grace Period) 설정 (예: 60초)
            min_hold_seconds = 60 
            if buy_time:
                time_since_buy = (datetime.now() - buy_time).total_seconds()
                if time_since_buy < min_hold_seconds:
                    if is_first_sell_check: # type: ignore
                        self.logger.debug(f"⏳ [{code}] 매수 후 {time_since_buy:.1f}초 경과. 매도 평가 유예 중 (최소 {min_hold_seconds}초)")
                    return signals # 유예 시간 동안 매도 신호 생성 안 함

            
            # 최고가 실시간 업데이트
            current_price = market_data.get('current_price', 0)
            if current_price > 0:
                # 최고가가 없거나 현재가가 더 높으면 업데이트
                if code not in self.trader.highest_prices:
                    self.trader.highest_prices[code] = current_price
                elif current_price > self.trader.highest_prices[code]:
                    old_highest = self.trader.highest_prices[code]
                    self.trader.highest_prices[code] = current_price
                    self.logger.info(f"📈 {code} 최고가 갱신: {old_highest:,}원 → {current_price:,}원")
                
                # 포트폴리오 딕셔너리에 업데이트된 최고가 반영
                portfolio['highest_prices'] = self.trader.highest_prices.copy() # type: ignore
            
            # 하드코딩된 이동 손절 로직을 제거하고 settings.ini의 전략 평가로 통합합니다.

            # 차트 데이터 가져오기 (틱/분봉)
            tic_chart_data = pd.DataFrame()
            min_chart_data = pd.DataFrame()
            previous_close = 0
            current_open = 0
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                cache_data = self.parent.chart_cache.get_cached_data(code)
                if cache_data:
                    tic_data = cache_data.get('tic_data', {})
                    min_data = cache_data.get('min_data', {})
                    previous_close = cache_data.get('previous_close', 0)
                    current_open = cache_data.get('current_open', 0)
                    if tic_data:
                        try:
                            tic_chart_data = pd.DataFrame(tic_data).dropna().reset_index(drop=True)
                        except Exception:
                            tic_chart_data = pd.DataFrame()
                    if min_data:
                        try:
                            min_chart_data = pd.DataFrame(min_data).dropna().reset_index(drop=True)
                        except Exception:
                            min_chart_data = pd.DataFrame()
            
            # 매도 전략 로드
            sell_strategies = []

            # 통합 전략: '급등주' + '갭상승' 전략을 합산 적용
            if strategy_name == "통합 전략":
                merged_sections = []
                if '급등주' in self.strategy_config:
                    merged_sections.append(('급등주', self.strategy_config['급등주']))
                if '갭상승' in self.strategy_config:
                    merged_sections.append(('갭상승', self.strategy_config['갭상승']))

                for section_name, section_conf in merged_sections:
                    items = [(k, v) for k, v in section_conf.items() if k.startswith('sell_stg_')]
                    items.sort(key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 999)
                    for key, value in items:
                        try:
                            strategy_data = json.loads(value)
                            if isinstance(strategy_data, dict) and 'name' in strategy_data:
                                strategy_data['name'] = f"[{section_name}] {strategy_data['name']}"
                            sell_strategies.append(strategy_data)
                        except json.JSONDecodeError:
                            self.logger.debug(f"매도 전략 파싱 실패 ({code}): {section_name}.{key}", exc_info=True)
                if is_first_sell_check and sell_strategies: # type: ignore
                    self.logger.debug(f"✅ [{code}] 통합 전략 로드 완료: 급등주+갭상승 매도 전략 {len(sell_strategies)}개")
            
            # strategy_config에서 현재 전략의 매도 조건 가져오기 (통합 전략이 아닌 경우)
            if not sell_strategies and strategy_name != "통합 전략" and strategy_name in self.strategy_config:
                strategy_conf = self.strategy_config[strategy_name]
                # sell_stg_로 시작하는 키들을 찾아서 파싱 (숫자 순서로 정렬)
                sell_stg_items = [(key, value) for key, value in strategy_conf.items() if key.startswith('sell_stg_')]
                # 숫자 순서로 정렬 (sell_stg_1, sell_stg_2, ... 순서)
                sell_stg_items.sort(key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 999)
                
                for key, value in sell_stg_items:
                    try:
                        strategy_data = json.loads(value)
                        sell_strategies.append(strategy_data)
                    except json.JSONDecodeError:
                        self.logger.debug(f"매도 전략 파싱 실패 ({code}): {key}", exc_info=True)
            
            # 전략이 없으면 기본 손절/익절 전략 사용
            if not sell_strategies:
                if is_first_sell_check: # type: ignore
                    self.logger.debug(f"⚠️ [{code}] 매도 전략 없음 - 기본 손익 전략 사용")
                # 기본 전략: -3% 손절, +5% 익절
                sell_strategies = [{
                    'name': '기본 손익 전략',
                    'conditions': [
                        {'type': 'stop_loss', 'value': -3.0},   # -3% 손절
                        {'type': 'take_profit', 'value': 5.0}   # +5% 익절
                    ]
                }]
            else:
                if is_first_sell_check: # type: ignore
                    self.logger.debug(f"✅ [{code}] 매도 전략 {len(sell_strategies)}개 로드됨: {strategy_name}")
            
            # 현재 수익률 계산 (전략 평가 전에)
            current_price = market_data.get('current_price', 0)
            profit_rate = (current_price - buy_price) / buy_price * 100 if buy_price > 0 else 0
            
            # 손절 조건 도달 시 디버그 로그 (자주 출력되지 않도록 조건부)
            if profit_rate < -0.6:  # 손절 기준 근처일 때만 디버그 # type: ignore
                self.logger.debug(f"🔍 [{code}] 손절 조건 도달 확인: 수익률={profit_rate:.2f}%, 매입가={buy_price:,}원, 현재가={current_price:,}원")
                self.logger.debug(f"🔍 [{code}] 로드된 매도 전략 수: {len(sell_strategies)}개")
                for idx, stg in enumerate(sell_strategies):
                    logging.debug(f"🔍 [{code}] 전략 {idx+1}: {stg.get('name', 'N/A')} - 조건: {stg.get('content', 'N/A')}")

            # strategy_utils를 사용하여 매도 전략 평가
            safe_locals = strategy_utils.prepare_sell_strategy_locals(
                code, tic_chart_data, min_chart_data, previous_close, current_open, 
                buy_price, buy_time, portfolio, 
                current_price=current_price,
                commission_rate=self.trader.commission_rate, 
                tax_rate=self.trader.tax_rate
            )
            condition_met, matched_strategy = strategy_utils.evaluate_strategies(
                sell_strategies, safe_locals, code, "매도"
            )
                
            if condition_met and matched_strategy:
                # 매도 조건 충족 시, 실제 주문 가능한 수량을 REST API로 최종 확인
                # 부분 익절 비율 확인
                partial_sell_ratio = matched_strategy.get('partial_sell_ratio')

                order_available_qty = 0
                try:
                    if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                        balance_result = await self.parent.login_handler.kiwoom_client.get_acnt_balance()
                        if balance_result:
                            api_holdings = balance_result.get('stk_acnt_evlt_prst', balance_result.get('output1', []))
                            for stock in api_holdings:
                                raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                                stock_code = self.parent.data_manager.normalize_stock_code(raw_code)
                                if stock_code == code:
                                    # 부분 익절인 경우, 보유 수량의 절반을 계산
                                    total_holding_qty = self.parent.data_manager.safe_int(stock.get('hldg_qty', 0))
                                    if partial_sell_ratio and 0 < partial_sell_ratio < 1:
                                        # 부분 익절 수량 계산 (소수점 버림)
                                        order_available_qty = int(total_holding_qty * partial_sell_ratio)
                                        self.logger.info(f"📡 부분 익절 수량 계산: 보유수량 {total_holding_qty}주 * {partial_sell_ratio} = {order_available_qty}주")
                                    else:
                                        # 전량 매도
                                        order_available_qty = self.parent.data_manager.safe_int(stock.get('rmnd_qty', 0))
                                        self.logger.info(f"전량 매도 수량 조회 (REST API): {code} 주문가능수량 {order_available_qty}주")
                                    break
                    # REST API 조회 실패 시 웹소켓 데이터로 대체
                    if order_available_qty <= 0:
                         if (hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'websocket_client')): # type: ignore
                            ws_balance_data = self.parent.login_handler.websocket_client.balance_data
                            if ws_balance_data and code in ws_balance_data:
                                order_available_qty = ws_balance_data[code].get('order_available_qty', 0)
                                if partial_sell_ratio and 0 < partial_sell_ratio < 1:
                                    total_holding_qty = ws_balance_data[code].get('quantity', 0)
                                    order_available_qty = int(total_holding_qty * partial_sell_ratio)
                                    self.logger.info(f"💰 부분 익절 수량 계산 (웹소켓 Fallback): 보유수량 {total_holding_qty}주 * {partial_sell_ratio} = {order_available_qty}주")
                                else:
                                    self.logger.info(f"전량 매도 수량 조회 (웹소켓 Fallback): {code} 주문가능수량 {order_available_qty}주")

                except Exception as qty_check_ex:
                    self.logger.error(f"주문가능수량 확인 중 오류 ({code}): {qty_check_ex}", exc_info=True)

                if order_available_qty <= 0:
                    self.logger.warning(f"⚠️ 매도 신호가 발생했으나 주문가능수량이 0주입니다. (다른 주문 처리 중일 수 있음): {code}")
                    return signals

                strategy_display_name = matched_strategy.get('name', strategy_name)
                self.logger.info(f"📉 매도 신호 발생: {code} - {strategy_display_name}")
                self.logger.debug(f"💰 매입가={buy_price:,}원, 현재가={current_price:,}원, 수익률={profit_rate:.2f}%")
                self.logger.debug(f"📊 보유수량={quantity:,}주, 주문가능수량={order_available_qty:,}주, 매도수량={order_available_qty:,}주")
                
                signals.append({
                    'strategy': matched_strategy.get('name', strategy_name),
                    'code': code,
                    'quantity': order_available_qty,  # 주문가능수량만큼만 매도
                    'price': 0,  # 시장가
                    'reason': f"기술적 지표 기반 매도 조건 충족: {matched_strategy.get('name', '')} (수익률: {profit_rate:.2f}%)"
                })
            else:
                # 매도 조건 미충족 시 현재 수익률 표시 (최초 1회만)
                if is_first_sell_check: # type: ignore
                    self.logger.debug(f"ℹ️ [{code}] 매도 조건 미충족 (보유 중, 수익률: {profit_rate:.2f}%)")
            
            return signals
            
        except Exception as ex:
            self.logger.error(f"매도 신호 생성 실패 ({code}): {ex}", exc_info=True)
            traceback.print_exc()
            return []
    
    async def execute_buy_signals(self, code, signals):
        """매수 신호 실행 (비동기)"""
        try:
            for signal in signals:
                success = await self.trader.place_buy_order(
                    code, 
                    signal['quantity'], 
                    signal['price'], 
                    signal['strategy']
                )
                
                if success:
                    self.signal_strategy_result.emit(
                        code, 
                        "buy", 
                        {
                            'strategy': signal['strategy'],
                            'reason': signal['reason'],
                            'quantity': signal['quantity'],
                            'price': signal['price']
                        }
                    )
                    
        except Exception as ex:
            self.logger.error(f"매수 신호 실행 실패 ({code}): {ex}", exc_info=True)
    
    async def execute_sell_signals(self, code, signals):
        """매도 신호 실행 (비동기)"""
        try:
            for idx, signal in enumerate(signals):
                requested_quantity = signal['quantity']
                
                # 실제 주문 전 주문가능수량 최종 확인 (웹소켓 실시간 데이터)
                actual_order_available_qty = requested_quantity  # 기본값
                try:
                    if (hasattr(self.parent, 'login_handler') and self.parent.login_handler and
                        hasattr(self.parent.login_handler, 'websocket_client') and self.parent.login_handler.websocket_client and
                        hasattr(self.parent.login_handler.websocket_client, 'balance_data')):
                        
                        ws_balance_data = self.parent.login_handler.websocket_client.balance_data
                        if ws_balance_data and code in ws_balance_data:
                            actual_order_available_qty = ws_balance_data[code].get('order_available_qty', requested_quantity)
                            if actual_order_available_qty < requested_quantity:
                                self.logger.warning(f"⚠️ [{code}] 주문가능수량 변경 감지: 요청={requested_quantity}주, 실제={actual_order_available_qty}주 (다른 주문으로 인한 변경)")
                except Exception as ws_check_ex:
                    self.logger.debug(f"주문 전 주문가능수량 체크 중 오류 ({code}): {ws_check_ex}", exc_info=True)
                
                # 주문가능수량이 0주 이하면 주문 스킵
                if actual_order_available_qty <= 0:
                    self.logger.warning(f"⚠️ [{code}] 주문가능수량 0주 - 주문 스킵 (다른 주문 처리 중)")
                    continue
                
                # 실제 주문 가능한 수량으로 제한
                final_quantity = min(requested_quantity, actual_order_available_qty)
                if final_quantity < requested_quantity:
                    self.logger.info(f"📊 [{code}] 주문 수량 조정: {requested_quantity}주 → {final_quantity}주")
                
                success = await self.trader.place_sell_order(
                    code, 
                    final_quantity,  # 조정된 수량 사용
                    signal['price'], 
                    signal['strategy']
                )
                
                if success:
                    self.logger.info(f"✅ [{code}] 매도 주문 성공: {signal['strategy']} - {final_quantity}주")
                    
                    self.signal_strategy_result.emit(
                        code, 
                        "sell", 
                        {
                            'strategy': signal['strategy'],
                            'reason': signal['reason'],
                            'quantity': final_quantity,  # 실제 주문된 수량
                            'price': signal['price']
                        }
                    )
                else:
                    self.logger.error(f"❌ [{code}] 매도 주문 실패: {signal['strategy']} - {final_quantity}주")
                    
        except Exception as ex:
            self.logger.error(f"매도 신호 실행 실패 ({code}): {ex}", exc_info=True)

# ==================== 자동매매 클래스 ====================
class AutoTrader(QObject):
    """자동매매 관리 클래스"""
    
    def __init__(self, trader, parent):
        try:
            super().__init__()           
            self.logger = logging.getLogger(self.__class__.__name__)
            self.trader = trader            
            self.parent = parent            
            self.is_running = True  # 자동매매 항상 활성화
            self.auto_liquidation_executed = False  # 15:15 자동 청산 실행 여부
            self.logger.debug("🔍 자동매매 실행 상태 초기화 완료 (항상 활성화)")
            
            # evaluation_interval 설정값으로 매매 판단 타이머 초기화
            self.trading_check_timer = QTimer()
            self.trading_check_timer.timeout.connect(self._periodic_trading_check)
            
            # 1분마다 거래 시간 감시 타이머 (거래 시간 외에 사용)
            self.time_monitor_timer = QTimer()
            self.time_monitor_timer.timeout.connect(self._check_trading_time)
            self.logger.debug(f"🔍 자동매매 초기화 완료 ({self.trader.evaluation_interval}초 주기 매매 판단)")
            self.logger.debug("자동매매 클래스 초기화 완료")
        except Exception as ex:
            self.logger.error(f"AutoTrader 초기화 실패: {ex}", exc_info=True)
            
            raise ex
    
    async def execute_auto_liquidation_async(self):
        """15:15 자동 청산 실행"""
        try:
            self.logger.info("🕒 15:15 자동 청산 로직 실행")
            if hasattr(self.parent, 'trading_manager'):
                await self.parent.trading_manager.sell_all_item()
        except Exception as ex:
            self.logger.error(f"❌ 자동 청산 실행 중 오류: {ex}", exc_info=True)
    
    def start_auto_trading(self):
        """자동매매 시작 (거래 시간: evaluation_interval, 거래 시간 외: 1분 타이머)"""
        try:
            if not self.is_running:
                self.is_running = True
                self.logger.debug("✅ 자동매매 활성화")
            
            # 현재 시간 체크
            now = datetime.now()
            current_hour = now.hour
            current_minute = now.minute
            current_time_minutes = current_hour * 60 + current_minute
            
            # 거래 시간: 9:00 ~ 15:30
            start_time_minutes = 9 * 60  # 540
            end_time_minutes = 15 * 60 + 30  # 930
            
            # 거래 시간 범위 확인
            if start_time_minutes <= current_time_minutes < end_time_minutes:
                # 거래 시간 내 - evaluation_interval 설정값 사용
                if not self.trading_check_timer.isActive():
                    interval_ms = self.trader.evaluation_interval * 1000  # 초 -> 밀리초
                    self.trading_check_timer.start(interval_ms)
                    self.logger.debug(f"✅ 자동매매 타이머 시작 ({self.trader.evaluation_interval}초 주기 - 거래 시간 내)")
                # 시간 모니터링 타이머는 중지
                if self.time_monitor_timer.isActive():
                    self.time_monitor_timer.stop()
            else:
                # 거래 시간 외 - 1분 모니터링 타이머 시작
                if not self.time_monitor_timer.isActive():
                    self.time_monitor_timer.start(60000)  # 1분 (60000ms)
                # 1초 타이머는 중지
                if self.trading_check_timer.isActive():
                    self.trading_check_timer.stop()
                
        except Exception as ex: # type: ignore
            self.logger.error(f"❌ 자동매매 시작 실패: {ex}")
    
    def stop_auto_trading(self):
        """자동매매 중지 (모든 타이머 정지)"""
        try:
            if self.is_running:
                self.is_running = False
                self.trading_check_timer.stop() # type: ignore
                self.time_monitor_timer.stop()
                self.logger.debug("🛑 자동매매 중지 (모든 타이머 정지)")
            else:
                self.logger.debug("자동매매가 이미 중지되어 있습니다")
                
        except Exception as ex:
            self.logger.error(f"❌ 자동매매 중지 실패: {ex}")
    
    @asyncSlot()
    async def _periodic_trading_check(self):
        """evaluation_interval 주기로 실행되는 주기적 매매 판단 (비동기)"""
        try:
            if not self.is_running:
                self.logger.debug("⚠️ 자동매매가 실행 중이 아닙니다")
                return
            
            # 시간 체크
            now = datetime.now()
            current_time_str = now.strftime("%H:%M")
            current_hour = now.hour
            current_minute = now.minute
            
            # 15:15 자동 청산 체크
            # 15:15에 자동 청산 (1회만 실행)
            if current_time_str == "15:15" and not self.auto_liquidation_executed:
                self.logger.info("🕒 15:15 도달 - 모든 보유 종목 자동 청산 시작")
                await self.execute_auto_liquidation_async()  # 청산 완료까지 대기
                self.auto_liquidation_executed = True  # 청산 완료 후 플래그 설정
            
            # 다음날을 위해 플래그 리셋 (15:31 이후)
            if current_time_str == "15:31" and self.auto_liquidation_executed:
                self.auto_liquidation_executed = False
                if hasattr(self, '_trading_stopped_logged'): # type: ignore
                    delattr(self, '_trading_stopped_logged')
                logging.debug("🔄 자동 청산 플래그 리셋 완료")
            
            # 자동매매 시간 제한: 9:00 ~ 15:30
            trading_start_time = (9, 0)  # 9:00
            trading_end_time = (15, 30)   # 15:30
            
            # 현재 시간이 거래 시간 범위 내인지 확인
            current_time_minutes = current_hour * 60 + current_minute
            start_time_minutes = trading_start_time[0] * 60 + trading_start_time[1]
            end_time_minutes = trading_end_time[0] * 60 + trading_end_time[1]
            
            # 거래 시간 범위 밖이면 타이머 중지하고 모니터링 타이머로 전환
            if current_time_minutes < start_time_minutes:
                # 9:00 이전 - 1초 타이머 중지, 1분 모니터링 타이머 시작
                if self.trading_check_timer.isActive():
                    self.trading_check_timer.stop() # type: ignore
                    self.logger.info(f"⏰ 1초 타이머 중지 (거래 시간 전: {current_time_str})")
                    self.logger.info(f"⏰ 자동매매 대기 중 (시작 시간: 09:00, 현재: {current_time_str})")
                if not self.time_monitor_timer.isActive():
                    self.time_monitor_timer.start(60000)  # 1분
                    self.logger.info("✅ 시간 모니터링 타이머 시작 (1분 주기)")
                return
            elif current_time_minutes >= end_time_minutes:
                # 15:30 이후 - 1초 타이머 중지, 1분 모니터링 타이머 시작
                if self.trading_check_timer.isActive():
                    self.trading_check_timer.stop() # type: ignore
                    self.logger.info(f"⏰ 1초 타이머 중지 (거래 종료)")
                    self.logger.info(f"⏰ 자동매매 종료됨 (종료 시간: 15:30, 현재: {current_time_str})")
                if not self.time_monitor_timer.isActive():
                    self.time_monitor_timer.start(60000)  # 1분
                    self.logger.info("✅ 시간 모니터링 타이머 시작 (1분 주기)")
                return
            else:
                # 거래 시간 내 - 모니터링 타이머는 중지
                if self.time_monitor_timer.isActive():
                    self.time_monitor_timer.stop() # type: ignore
            
            # chart_cache가 있는지 확인
            if not hasattr(self.parent, 'chart_cache') or not self.parent.chart_cache:
                # 최초 1회만 로그 출력
                if not hasattr(self, '_cache_missing_logged') or not self._cache_missing_logged:
                    logging.warning("⚠️ chart_cache가 없어서 매매 판단을 실행할 수 없습니다")
                    self._cache_missing_logged = True
                return
            
            # 주기적 상태 로그 (1분에 1번)
            if not hasattr(self, '_last_status_log_time'):
                self._last_status_log_time = 0
            
            current_time = time.time()
            if current_time - self._last_status_log_time >= 60:
                monitoring_codes = list(self.parent.chart_cache.cache.keys())
                if monitoring_codes: # type: ignore
                    self.logger.debug(f"🔍 자동매매 모니터링 중: {len(monitoring_codes)}개 종목 - {monitoring_codes}")
                else:
                    self.logger.debug("🔍 자동매매 실행 중 - 모니터링 종목 없음")
                self._last_status_log_time = current_time
            
            # 15:15 자동 청산 이후에는 매매 중지
            if self.auto_liquidation_executed:
                # 최초 1회만 로그 출력
                if not hasattr(self, '_trading_stopped_logged'): # type: ignore
                    self.logger.info("⏹️ 15:15 자동 청산 완료 - 모든 매매 활동 중지")
                    self._trading_stopped_logged = True
                return
            
            # 모니터링 중인 모든 종목에 대해 매매 판단 실행 (동시 처리)
            codes = list(self.parent.chart_cache.cache.keys()) # type: ignore
            if codes:
                # 병렬 처리하여 성능 향상
                await asyncio.gather(
                    *[self._analyze_and_execute_trading_async(code) for code in codes],
                    return_exceptions=True
                )
                    
        except Exception as ex:
            self.logger.error(f"주기적 매매 판단 중 오류: {ex}", exc_info=True)
    
    async def _analyze_and_execute_trading_async(self, code):
        """매매 판단 및 실행 (비동기)"""
        try:
            await self.analyze_and_execute_trading(code)
        except Exception as ex:
            self.logger.error(f"비동기 매매 판단 실패 ({code}): {ex}", exc_info=True)
    
    def _check_trading_time(self):
        """거래 시간 감시 (1분마다 실행) - 거래 시간이 되면 1초 타이머로 전환"""
        try:
            now = datetime.now()
            current_time_str = now.strftime("%H:%M")
            current_hour = now.hour
            current_minute = now.minute
            current_time_minutes = current_hour * 60 + current_minute
            
            # 거래 시간: 9:00 ~ 15:30
            start_time_minutes = 9 * 60  # 540
            end_time_minutes = 15 * 60 + 30  # 930
            
            # 거래 시간 범위 확인
            if start_time_minutes <= current_time_minutes < end_time_minutes:
                # 거래 시간 도달 - 1분 모니터링 타이머 중지, evaluation_interval 타이머 시작
                if self.time_monitor_timer.isActive():
                    self.time_monitor_timer.stop() # type: ignore
                    self.logger.info("⏰ 시간 모니터링 타이머 중지 (거래 시간 도달)")
                
                if not self.trading_check_timer.isActive():
                    interval_ms = self.trader.evaluation_interval * 1000  # 초 -> 밀리초
                    self.trading_check_timer.start(interval_ms) # type: ignore
                    self.logger.info(f"🚀 자동매매 시작 ({self.trader.evaluation_interval}초 주기, 거래 시간 도달: {current_time_str})")
            else:
                # 거래 시간 외 - 계속 대기
                self.logger.debug(f"⏰ 거래 시간 대기 중 (현재: {current_time_str})")
            
        except Exception as ex:
            self.logger.error(f"거래 시간 체크 중 오류: {ex}", exc_info=True)
    
    async def analyze_and_execute_trading(self, code):
        """ChartDataCache 데이터로 매매 판단 및 실행 (AutoTrader에서 통합 관리) (비동기)
        KiwoomStrategy.evaluate_strategy를 사용하여 매매를 판단합니다.
        """
        try:
            # 15:15 자동 청산 이후에는 매매 중지
            if self.auto_liquidation_executed:
                return False
            
            # 거래 시간 체크 (9:00 ~ 15:30)
            now = datetime.now()
            current_hour = now.hour
            current_minute = now.minute
            current_time_minutes = current_hour * 60 + current_minute
            
            # 9:00 ~ 15:30 범위 체크
            if current_time_minutes < 540 or current_time_minutes >= 930:  # 540 = 9*60, 930 = 15*60 + 30
                return False
            
            # 디버그 로그: 최초 1회만 출력 (종목별)
            if not hasattr(self, '_analyze_debug_codes'):
                self._analyze_debug_codes = set()
            
            is_first_debug = code not in self._analyze_debug_codes
            if is_first_debug:
                self.logger.debug(f"🔍 [{code}] 매매 판단 시작")
                self._analyze_debug_codes.add(code)
            
            # chart_cache에서 데이터 가져오기
            if not hasattr(self.parent, 'chart_cache') or not self.parent.chart_cache:
                if is_first_debug:
                    logging.debug(f"ℹ️ [{code}] chart_cache 초기화 대기 중")
                return False
            
            cache_data = self.parent.chart_cache.get_cached_data(code)
            if not cache_data:
                if is_first_debug:
                    logging.debug(f"ℹ️ [{code}] 캐시 데이터 수집 대기 중")
                return False
            
            tic_data = cache_data.get('tic_data', {})
            min_data = cache_data.get('min_data', {})
            
            if not tic_data or not min_data:
                if is_first_debug:
                    has_tic = bool(tic_data and tic_data.get('close'))
                    has_min = bool(min_data and min_data.get('close'))
                    tic_len = len(tic_data.get('close', [])) if tic_data else 0
                    min_len = len(min_data.get('close', [])) if min_data else 0
                    self.logger.debug(f"ℹ️ [{code}] 차트 데이터 수집 대기 중 (틱:{has_tic}({tic_len}개), 분:{has_min}({min_len}개))")
                return False

            # KiwoomStrategy.evaluate_strategy를 사용하여 매매 판단
            if not hasattr(self.parent, 'objstg') or not self.parent.objstg:
                if is_first_debug:
                    self.logger.debug(f"ℹ️ [{code}] 전략 객체 초기화 대기 중")
                return False
            
            # 캐시에서 전일종가 가져오기
            previous_close = cache_data.get('previous_close', 0)
            
            # market_data 구성
            current_price = tic_data.get('close', [0])[-1] if tic_data.get('close') else 0
            volume = tic_data.get('volume', [0])[-1] if tic_data.get('volume') else 0

            market_data = {
                'tic_data': tic_data,
                'min_data': min_data,
                'current_price': current_price,
                'volume': volume,
                'change_rate': 0,  # 이 값은 현재 알 수 없으므로 0으로 설정
                'previous_close': previous_close  # 전일종가 (캐시에서 한 번만 조회한 값)
            }
            
            if is_first_debug:
                self.logger.debug(f"✅ [{code}] 매매 판단 데이터 준비 완료 (현재가:{current_price:,}, 거래량:{volume:,})")
            
            # 전략 평가 실행
            await self.parent.objstg.evaluate_strategy(code, market_data)
            return True

        except Exception as ex:
            self.logger.error(f"매매 판단 및 실행 실패 ({code}): {ex}", exc_info=True)
            
            return False
    
    def _check_risk_management(self, signal_type, signal_data):
        """리스크 관리 확인"""
        try:
            # 매수 시: 기본적인 데이터 유효성만 확인 (실제 현금 확인은 _execute_buy_order에서 수행)
            if signal_type == 'buy':
                required_amount = signal_data.get('amount', 0)
                if required_amount <= 0:
                    self.logger.warning(f"매수 금액이 유효하지 않음: {required_amount}")
                    return False
                self.logger.debug(f"매수 신호 확인: 필요 금액 {required_amount}")
            
            # 매도 시: 웹소켓 실시간 잔고 데이터로 보유 종목 확인
            elif signal_type == 'sell':
                balance_data = self.trader.get_balance_data()
                if not balance_data:
                    self.logger.warning("웹소켓 잔고 데이터가 없습니다")
                    return False
                
                code = signal_data.get('code')
                holdings = balance_data.get('holdings', {})
                if code not in holdings or holdings[code].get('quantity', 0) <= 0:
                    self.logger.warning(f"보유 종목 없음: {code}")
                    return False
            
            # 손절/익절 확인
            if not self._check_stop_loss_take_profit(signal_type, signal_data):
                return False
            
            self.logger.debug(f"리스크 관리 확인 통과: {signal_type}")
            return True
            
        except Exception as ex:
            self.logger.error(f"리스크 관리 확인 실패: {ex}", exc_info=True)
            return False
    
    def _check_stop_loss_take_profit(self, signal_type, signal_data):
        """손절/익절 확인"""
        try:
            # 손절/익절 로직 구현
            # 현재는 기본적으로 통과
            return True
        except Exception as ex:
            self.logger.error(f"손절/익절 확인 실패: {ex}", exc_info=True)
            return False
    
# ==================== 로그인 핸들러 ====================
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
                        # 트레이더 객체 생성 로그 제거
                        
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

# ==================== MyWindow Manager 클래스들 ====================

class DataManager:
    """데이터 조회 및 관리 매니저"""
    
    def __init__(self, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
    
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
                    # 모의투자 여부에 따라 서버 URL 선택
                    server_url = kiwoom_client.mock_url if kiwoom_client.is_mock else kiwoom_client.base_url
                    data = {'mrkt_tp': market_code}
                    url = f"{server_url}/api/dostk/stkinfo"
                    
                    response = await kiwoom_client.client.post(url, headers=headers, json=data, timeout=10.0)

                    if response.status_code == 200: # type: ignore
                        result = response.json()
                        if result.get('return_code') == 0 and 'list' in result:
                            for stock_info in result['list']:
                                if stock_info.get('name') == stock_name:
                                    stock_code = stock_info.get('code')
                                    if stock_code:
                                        self.logger.info(f"✅ 종목명 검색 성공: {stock_name} -> {stock_code}")
                                        return stock_code
                        else:
                            self.logger.warning(f"종목 리스트 조회 실패 (시장: {market_code}): {result.get('return_msg')}")
                    else:
                        self.logger.error(f"종목 리스트 API 호출 실패 (시장: {market_code}): HTTP {response.status_code}")

            return None
            
        except Exception as ex:
            self.logger.error(f"종목명 검색 실패 ({stock_name}): {ex}")
            return None


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
        """모니터링 리스트와 관련된 모든 상태를 초기화합니다."""
        self.logger.info("🔄 투자 전략 변경으로 인해 기존 모니터링 목록을 초기화합니다.")
        
        # 1. 서버에 등록된 *모든* 조건검색을 해제 시도 (상태 불일치 해결)
        if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
            all_seqs = [c['seq'] for c in self.parent.condition_search_list]
            self.logger.debug(f"🔍 모든 조건검색 해제 시도: {all_seqs}")
            for seq in all_seqs:
                # 해제 요청을 보냅니다 (서버에 등록 안되어있으면 무시됨).
                await self.parent.stop_condition_realtime(seq)
        
        # 2. 클라이언트의 활성 목록도 비웁니다.
        if hasattr(self.parent, 'active_realtime_conditions'):
            self.parent.active_realtime_conditions.clear()

        self.logger.debug("✅ 모든 실시간 조건검색 모니터링 중단 완료.")

        # 2. 모니터링 리스트 박스 비우기
        self.parent.trading_tab.monitoringBox.clear()
        
        # 3. 차트 캐시 및 관련 데이터 초기화
        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
            self.parent.chart_cache.stop() # stop() 메서드가 캐시를 비우고 타이머를 중지

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
    
    async def sell_all_item(self):
        """전체 매도 (키움 REST API 기반) (비동기)"""
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
        self.logger.info("수동 전체 매도 시작 - 모든 주기적 타이머 일시 중지")
        try:
            async with self.parent.trading_lock:
                if self.parent.trading_tab.boughtBox.count() == 0:
                    self.logger.warning("매도할 종목이 없습니다.")
                    # 타이머 재시작
                    self._restart_timers_after_manual_trade(autotrader, chart_cache)
                    return

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
                                            stock_code = self.parent.normalize_stock_code(raw_code)
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
                        
                        # 매도 주문 실행
                        if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                            success = await self.parent.login_handler.kiwoom_client.place_sell_order(code, quantity, 0, "market")
                            if success:
                                success_count += 1
                            # API 요청 제한을 피하기 위해 약간의 지연 추가
                            if len(sell_items) > 1:
                                await asyncio.sleep(0.5)

                                self.logger.info(f"✅ 전체 매도 성공: {code} {quantity}주")
                            else:
                                self.logger.error(f"❌ 전체 매도 실패: {code}")
                    except Exception as item_ex:
                        self.logger.error(f"❌ {code} 매도 중 오류: {item_ex}")
                
                # 결과 로그
                if success_count > 0:
                    self.logger.info(f"✅ 전체 매도 완료: {success_count}개 종목")
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
                
                self.logger.info(f"💰 전량 매도 실행: {code} {quantity}주")
                
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


class BacktestManager:
    """백테스팅 관리 매니저"""
    
    def __init__(self, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
    
    def load_backtest_strategies(self):
        """백테스팅 전략 콤보박스 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            self.parent.backtest_tab.bt_strategy_combo.clear()
            if config.has_section('STRATEGIES'):
                for key, value in config.items('STRATEGIES'):
                    if key.startswith('stg_') or key == 'stg_integrated':
                        self.parent.backtest_tab.bt_strategy_combo.addItem(value)
            
            # 기본 전략 설정
            if config.has_option('SETTINGS', 'last_strategy'):
                last_strategy = config.get('SETTINGS', 'last_strategy') # type: ignore
                index = self.parent.backtest_tab.bt_strategy_combo.findText(last_strategy)
                if index >= 0:
                    self.parent.backtest_tab.bt_strategy_combo.setCurrentIndex(index)
            
            self.logger.debug("백테스팅 전략 콤보박스 로드 완료")
            
        except Exception as ex:
            self.logger.error(f"백테스팅 전략 콤보박스 로드 실패: {ex}")
    
    def load_db_period(self):
        """DB 기간 불러오기"""
        try:
            # KiwoomBacktester 인스턴스를 생성하여 DB 경로를 올바르게 참조
            # DB 파일 경로는 backtester.py에 정의되어 있음
            backtester = KiwoomBacktester(db_path='stock_data.db')
            start_date, end_date = backtester.get_db_data_range()

            if start_date and end_date:
                self.parent.backtest_tab.bt_start_date.setText(start_date)
                self.parent.backtest_tab.bt_end_date.setText(end_date)
                self.logger.debug(f"DB 기간 로드: {start_date} ~ {end_date}")
            else: # type: ignore
                self.logger.warning("DB에서 날짜 정보를 찾을 수 없습니다.")
                
        except Exception as ex:
            self.logger.error(f"DB 기간 로드 실패: {ex}")
    
    def run_backtest(self):
        """백테스팅 실행"""
        try:
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

            self.logger.info(f"백테스팅 시작: {strategy_name} ({start_date} ~ {end_date})") # type: ignore
            self.parent.backtest_tab.bt_results_text.clear()
            self.parent.backtest_tab.bt_results_text.append(f"백테스팅을 시작합니다: {strategy_name}\n")
            QApplication.processEvents()

            # 4. 백테스팅 대상 종목 가져오기 (모니터링 중인 종목 사용) # type: ignore
            codes = self.parent.monitoring_manager.get_monitoring_stock_codes()
            if not codes:
                self.logger.warning("백테스팅 오류: 백테스팅을 실행할 모니터링 대상 종목이 없습니다.")
                return

            self.parent.backtest_tab.bt_results_text.append(f"대상 종목: {', '.join(codes)}\n")
            QApplication.processEvents()

            # 5. 백테스팅 실행 # type: ignore
            success = backtester.run_backtest(codes, start_date, end_date, strategy_name)

            # 6. 결과 표시
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
            else:
                self.parent.backtest_tab.bt_results_text.append("\n백테스팅 실행에 실패했거나 결과가 없습니다.")

        except Exception as ex:
            self.logger.error(f"백테스팅 실행 실패: {ex}")
            # QMessageBox.critical(self.parent, "백테스팅 오류", f"백테스팅 실행 중 오류가 발생했습니다:\n{ex}")


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

            # 2. REST API 잔고조회
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
        self.counterlabel = QLabel('타이머: 0')
        self.counterlabel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        strategyLayout.addWidget(self.counterlabel)
        self.chart_status_label = QLabel("Chart: None")
        self.chart_status_label.setProperty("class", "disconnected")
        self.chart_status_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        strategyLayout.addWidget(self.chart_status_label)

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
        
        # 시작일
        period_layout.addWidget(QLabel("시작일:"))
        self.bt_start_date = QLineEdit()
        self.bt_start_date.setPlaceholderText("YYYYMMDD")
        self.bt_start_date.setFixedWidth(120)
        period_layout.addWidget(self.bt_start_date)
        
        # 종료일
        period_layout.addWidget(QLabel("종료일:"))
        self.bt_end_date = QLineEdit()
        self.bt_end_date.setPlaceholderText("YYYYMMDD")
        self.bt_end_date.setFixedWidth(120)
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


# ==================== 메인 윈도우 ====================
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
            if self._post_login_setup_done:
                self.logger.debug("post_login_setup이 이미 실행되었습니다. 건너뜁니다.")
                return

            # 1. 트레이더 객체 확인 (이미 API 연결 시 생성됨)
            if not hasattr(self, 'trader') or not self.trader:
                self.logger.warning("⚠️ 트레이더 객체가 없습니다. API 연결을 확인해주세요.")
                return # type: ignore

            # 2. 전략 객체 초기화
            if not self.objstg:
                self.objstg = KiwoomStrategy(self.trader, self)
                self.logger.debug("🔍 KiwoomStrategy 객체 생성 완료")

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


# ==================== 메인 실행 ====================
async def main():
    """메인 실행 함수 - qasync를 사용한 비동기 처리"""
    global main_window
    print("프로그램 시작")
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
        # QRect를 QRectF로 변환
        rect = self.picture.boundingRect()
        return pg.QtCore.QRectF(rect)

# ==================== PyQtGraph 차트 위젯 클래스 ====================
class PyQtGraphWidget(pg.PlotWidget):
    """PyQtGraph 기반 차트 위젯"""
    def __init__(self, parent=None, title="실시간 차트"):
        self.logger = logging.getLogger(self.__class__.__name__)
        super().__init__(parent)
        
        # 차트 설정
        self.setTitle(title)
        self.showGrid(x=True, y=False, alpha=0.5)
        
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
                self.legend_item = LegendItem(offset=(left_x, top_y), size=(legend_width, legend_height))
                
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
            
            # 실제 데이터에서 분 단위를 확인하여 동적으로 레이블 간격 설정
            minutes_in_data = set()
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
                        # 기본 시간 설정
                        dt = datetime.now()
                    
                    minute = dt.minute
                    minutes_in_data.add(minute)
                    
                except Exception as e:
                    continue
            
            # 데이터에 있는 분 단위를 기반으로 레이블 간격 설정
            if minutes_in_data:
                # 30분 단위로 가장 가까운 분들을 찾기
                if chart_type == "tic":
                    # 틱차트: 30분 단위로 가장 가까운 분들
                    target_minutes = [0, 30]
                elif chart_type == "minute":
                    # 분차트: 30분 단위로 가장 가까운 분들
                    target_minutes = [0, 30]
                else:
                    # 기본: 30분 단위
                    target_minutes = [0, 30]
                
                # 실제 데이터에 있는 분들 중에서 목표 분에 가장 가까운 것들 선택
                label_intervals = []
                for target in target_minutes:
                    closest_minute = min(minutes_in_data, key=lambda x: abs(x - target))
                    if abs(closest_minute - target) <= 15:  # 15분 이내 차이면 허용
                        label_intervals.append(closest_minute)
                
                # 만약 30분 단위에 해당하는 데이터가 없으면 모든 데이터의 분을 사용
                if not label_intervals:
                    label_intervals = sorted(list(minutes_in_data))
                    # 너무 많으면 간격을 두고 선택
                    if len(label_intervals) > 10:
                        step = len(label_intervals) // 5
                        label_intervals = label_intervals[::step]
            else:
                # 데이터가 없으면 기본값 사용
                label_intervals = [0, 30]
            
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
                        # 기본 시간 설정
                        dt = datetime.now()
                    
                    minute = dt.minute
                    
                    label = ""
                    if minute in label_intervals and minute != last_label_minute:
                        last_label_minute = minute
                        label = dt.strftime("%H:%M")
                    elif minute not in label_intervals:
                        last_label_minute = -1
                    
                    if label:
                        tics.append((i, label))  # (X축 인덱스, 표시할 텍스트)
                        
                except Exception as e:
                    self.logger.debug(f"X축 레이블 설정 중 오류 (무시됨): {e}")
                    continue
            
            # pyqtgraph는 겹치는 레이블을 자동으로 숨겨 "..." 문제가 발생하지 않음
            if tics:
                axis.setTicks([tics])
                self.logger.debug(f"🔍 PyQtGraphWidget X축 레이블 설정 완료: {len(tics)}개 레이블 ({chart_type} 차트)")
                
        except Exception as ex:
            self.logger.error(f"❌ X축 레이블 설정 실패: {ex}")

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
            self.logger.debug("✅ 틱 차트 위젯 업데이트 완료")
                                          
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
            self.logger.debug("✅ 분봉 차트 위젯 업데이트 완료")
                                          
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

class ChartDataCache(QObject):
    """모니터링 종목 차트 데이터 메모리 캐시 클래스"""
    
    # 시그널 정의
    data_updated = pyqtSignal(str)  # 특정 종목 데이터 업데이트
    cache_cleared = pyqtSignal()    # 캐시 전체 정리
    
    def __init__(self, trader, parent):
        try:
            self.logger = logging.getLogger(self.__class__.__name__)
            super().__init__(parent)            
            self.trader = trader            
            self.parent = parent  # MyWindow 객체 저장
            self.cache = {}  # {종목코드: {'tic_data': {}, 'min_data': {}, 'last_update': datetime}}
            self.api_request_count = 0  # API 요청 카운터
            self.last_api_request_time = 0  # 마지막 API 요청 시간
            
            # API 요청 큐 시스템
            self.api_request_queue = []  # API 요청 큐
            self.queue_processing = False  # 큐 처리 중 플래그
            self.queue_timer = None  # 큐 처리 타이머
            self.active_chart_tasks = {} # 활성 차트 데이터 수집 asyncio 태스크 관리
            self.pending_stocks = {}  # 큐에 대기 중인 종목 정보 (코드: 이름)
            self.logger.debug("🔍 API 요청 큐 시스템 초기화 완료")
            
            # QTimer 객체를 즉시 생성
            self.update_timer = QTimer(self)
            self.save_timer = QTimer(self)
            self.queue_timer = QTimer(self)
            self.logger.debug("🔍 타이머 객체 즉시 생성 완료")
            
            self.logger.debug("📊 차트 데이터 캐시 초기화 완료")
        except Exception as ex:
            self.logger.error(f"❌ ChartDataCache 초기화 실패: {ex}", exc_info=True)
            raise ex
    

    def collect_chart_data_async(self, code, max_retries=3):
        """비동기 차트 데이터 수집 (asyncio 기반, qasync 통합)"""
        try:
            # qasync 환경에서 메인 이벤트 루프 사용 시도
            try:
                loop = asyncio.get_running_loop()
                # 이미 실행 중인 이벤트 루프가 있으면 태스크로 실행
                task = asyncio.create_task(self._collect_chart_data_internal(code, max_retries))
                self.active_chart_tasks[code] = task
                self.logger.debug(f"✅ 차트 데이터 수집 태스크 시작: {code} (활성 태스크 수: {len(self.active_chart_tasks)})")
                return
            except RuntimeError:
                # 실행 중인 이벤트 루프가 없음 - ThreadPoolExecutor로 fallback
                self.logger.debug(f"⚠️ 실행 중인 이벤트 루프가 없어 ThreadPoolExecutor 사용: {code}")
                pass
            
            # Fallback: ThreadPoolExecutor 사용 (이벤트 루프가 없는 경우)
            def run_collect():
                try:
                    # 새로운 이벤트 루프 생성
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        # 비동기 데이터 수집 실행
                        return loop.run_until_complete(self._collect_chart_data_internal(code, max_retries))
                    finally:
                        loop.close()
                except Exception as e:
                    self.logger.error(f"차트 데이터 수집 실행 오류: {e}")
                    return None
            
            # 별도 스레드에서 데이터 수집 실행
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_collect)
                future.result(timeout=60)  # 60초 타임아웃
            
        except Exception as ex:
            self.logger.error(f"❌ 비동기 차트 데이터 수집 실패: {code} - {ex}")
    
    async def _collect_chart_data_internal(self, code, max_retries=3):
        """내부 차트 데이터 수집 (asyncio 기반)"""
        # 수동 매매 작업이 진행 중이면 데이터 수집을 건너뜁니다.
        if hasattr(self.parent, 'trading_lock') and self.parent.trading_lock.locked():
            self.logger.debug(f"수동 매매 작업 진행 중 - 차트 데이터 수집 건너뜀: {code}")
            return

        try:
            self.logger.debug(f"📊 차트 데이터 수집 시작: {code}")
            
            # 비동기로 틱 데이터와 분봉 데이터 수집
            tic_data, min_data = await asyncio.gather(
                self._collect_tic_data_async(code, max_retries),
                self._collect_minute_data_async(code, max_retries),
                return_exceptions=True
            )
            
            # 예외 처리
            if isinstance(tic_data, Exception):
                self.logger.error(f"틱 데이터 수집 실패: {tic_data}")
                tic_data = None
            if isinstance(min_data, Exception):
                self.logger.error(f"분봉 데이터 수집 실패: {min_data}")
                min_data = None
            
            # 틱 데이터가 None인 경우 빈 딕셔너리로 초기화
            if tic_data is None:
                tic_data = {'time': [], 'open': [], 'high': [], 'low': [], 'close': [], 'volume': [], 'strength': []}
                self.logger.warning(f"틱 데이터가 None입니다. 빈 데이터로 초기화: {code}")
                
            # 분봉 데이터가 None인 경우 빈 딕셔너리로 초기화
            if min_data is None:
                min_data = {'time': [], 'open': [], 'high': [], 'low': [], 'close': [], 'volume': []}
                self.logger.warning(f"분봉 데이터가 None입니다. 빈 데이터로 초기화: {code}")
            
            # 기술적 지표 계산 (비동기, ThreadPoolExecutor 사용)
            try:
                loop = asyncio.get_running_loop()
                # 틱 데이터와 분봉 데이터를 병렬로 계산
                if tic_data and min_data:
                    tic_data, min_data = await asyncio.gather(
                        loop.run_in_executor(None, self._calculate_technical_indicators, tic_data, "tic"),
                        loop.run_in_executor(None, self._calculate_technical_indicators, min_data, "minute"),
                        return_exceptions=True
                    )
                    # 예외 처리
                    if isinstance(tic_data, Exception):
                        self.logger.error(f"틱 지표 계산 실패: {tic_data}")
                        tic_data = None
                    if isinstance(min_data, Exception):
                        self.logger.error(f"분봉 지표 계산 실패: {min_data}")
                        min_data = None
                elif tic_data:
                    tic_data = await loop.run_in_executor(None, self._calculate_technical_indicators, tic_data, "tic")
                elif min_data:
                    min_data = await loop.run_in_executor(None, self._calculate_technical_indicators, min_data, "minute")
            except RuntimeError:
                # 이벤트 루프가 없으면 동기로 계산
                if tic_data:
                    tic_data = self._calculate_technical_indicators(tic_data, "tic")
                if min_data:
                    min_data = self._calculate_technical_indicators(min_data, "minute")
            except Exception as calc_ex:
                self.logger.error(f"기술적 지표 계산 중 오류: {calc_ex}")
            
            # 메인 스레드에서 콜백 실행
            QTimer.singleShot(0, lambda: self._on_chart_data_ready(code, tic_data, min_data))
            
        except Exception as e:
            self.logger.error(f"차트 데이터 수집 실패 ({code}): {e}")
            QTimer.singleShot(0, lambda: self._on_chart_data_error(code, str(e)))
        finally:
            # 태스크 완료 처리
            if code in self.active_chart_tasks:
                self.active_chart_tasks.pop(code)
            
            # 큐 처리 플래그 해제
            if self.queue_processing:
                self.queue_processing = False
                self.logger.debug(f"✅ 큐 처리 플래그 해제: {code}")
                self.logger.debug(f"✅ 차트 데이터 수집 태스크 정리 완료: {code}")
    
    async def _collect_tic_data_async(self, code, max_retries=3):
        """틱 데이터 수집 (asyncio 기반)"""
        for attempt in range(max_retries):
            try:
                # API 제한 확인 (비동기 버전)
                await ApiLimitManager.check_api_limit_and_wait_async(request_type='tic')
                
                # 비동기 API 직접 호출
                data = await self.trader.client.get_stock_tic_chart(code, tic_scope=60)
                
                if data:
                    return data
                    
            except Exception as e:
                self.logger.warning(f"틱 데이터 수집 시도 {attempt + 1}/{max_retries} 실패: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
        
        return None
    
    async def _collect_minute_data_async(self, code, max_retries=3):
        """분봉 데이터 수집 (asyncio 기반)"""
        for attempt in range(max_retries):
            try:
                # API 제한 확인 (비동기 버전)
                await ApiLimitManager.check_api_limit_and_wait_async(request_type='minute')
                
                # 비동기 API 직접 호출
                data = await self.trader.client.get_stock_minute_chart(code, period=3)
                
                if data:
                    return data
                    
            except Exception as e:
                self.logger.warning(f"분봉 데이터 수집 시도 {attempt + 1}/{max_retries} 실패: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
        
        return None
    
    def _on_chart_data_ready(self, code, tic_data, min_data):
        """차트 데이터 수집 완료 시그널 핸들러"""
        try:
            self.logger.debug(f"✅ 차트 데이터 수집 완료: {code} (tic: {tic_data is not None}, min: {min_data is not None})")
            
            # 캐시에 데이터 저장
            if code not in self.cache:
                self.cache[code] = {
                    'tic_data': None,
                    'min_data': None,
                    'last_update': None,
                    'last_save': None,
                    'previous_close': 0  # 전일종가 (한 번만 조회)
                }
                self.logger.debug(f"📝 {code}: 캐시 초기화")
            
            # 기술적 지표 계산은 이미 _collect_chart_data_internal에서 완료됨
            # _on_chart_data_ready는 캐시 저장 및 UI 업데이트만 담당
            
            self.cache[code]['tic_data'] = tic_data
            self.cache[code]['min_data'] = min_data
            self.cache[code]['last_update'] = datetime.now()
            
            self.logger.debug(f"💾 {code}: 캐시에 데이터 저장 완료 (총 캐시: {len(self.cache)}개 종목)")
            
            # 데이터 업데이트 시그널 발생
            self.data_updated.emit(code)
            
            # API 큐에서 처리된 종목을 모니터링 리스트박스에 추가
            if code in self.pending_stocks:
                stock_name = self.pending_stocks[code]
                if hasattr(self, 'parent') and self.parent:
                    # 이미 모니터링에 존재하는지 확인 (중복 추가 방지)
                    already_exists = False
                    for i in range(self.parent.trading_tab.monitoringBox.count()):
                        item_text = self.parent.trading_tab.monitoringBox.item(i).text()
                        # 종목코드 추출                        
                        if item_text == code:
                            already_exists = True
                            self.logger.debug(f"ℹ️ 이미 모니터링에 존재하여 추가 건너뜀: {code} - {stock_name}")
                            break
                    
                    # 존재하지 않을 때만 추가
                    if not already_exists:
                        # UI와 실시간 구독만 처리하도록 MonitoringManager 호출
                        asyncio.create_task(self.parent.monitoring_manager.add_stock_to_monitoring(code, stock_name))
                        self.logger.debug(f"✅ 모니터링 리스트박스에 추가 완료: {code} - {stock_name}")
                
                # pending_stocks에서 제거
                del self.pending_stocks[code]
            
            # 데이터 수집 결과 로그 (간소화)
            if not tic_data and not min_data:
                self.logger.warning(f"⚠️ 차트 데이터 수집 실패: {code}")
            
        except Exception as ex:
            self.logger.error(f"❌ 차트 데이터 처리 실패: {code} - {ex}", exc_info=True)
    
    def _on_chart_data_error(self, code, error_message):
        """차트 데이터 수집 에러 시그널 핸들러"""
        try:
            self.logger.error(f"❌ 차트 데이터 수집 에러: {code} - {error_message}")
        except Exception as ex:
            self.logger.error(f"❌ 차트 데이터 에러 처리 실패: {code} - {ex}")
    
    async def add_monitoring_stock(self, code):
        """모니터링 종목 추가"""
        try:            
            if code not in self.cache:
                # 전일종가 조회 (ka10100 API)
                current_open = 0
                previous_close = 0
                if hasattr(self.parent, 'login_handler') and self.parent.login_handler and self.parent.login_handler.kiwoom_client:
                    try:
                        stock_info = await self.parent.login_handler.kiwoom_client.get_stock_info_ka10100(code)
                        if stock_info:
                            if 'lastPrice' in stock_info:
                                previous_close = int(stock_info['lastPrice'])
                                self.logger.info(f"📊 {code} 전일종가 조회 완료: {previous_close:,}원")
                            if 'openPrice' in stock_info:
                                current_open = int(stock_info['openPrice'])
                                self.logger.info(f"📊 {code} 당일시가 조회 완료: {current_open:,}원")
                    except Exception as e:
                        self.logger.error(f"❌ {code} 전일종가 조회 중 오류: {e}")
                
                self.cache[code] = {
                    'tic_data': None,
                    'min_data': None,
                    'last_update': None,
                    'last_save': None,
                    'previous_close': previous_close,  # 전일종가 (한 번만 조회)
                    'current_open': current_open      # 당일시가 (한 번만 조회)
                }
                self.logger.debug(f"✅ 모니터링 종목 추가 완료: {code}")
                
                # 종목코드만 저장 (API 호출 제거)
                self.pending_stocks[code] = f"종목{code}"
                
                # update_timer 시작 (첫 번째 종목이 추가될 때)
                if hasattr(self, 'update_timer') and self.update_timer:
                    if not self.update_timer.isActive():
                        # 차트 업데이트 주기는 chartdata_update_interval 사용 (기본 10초)
                        chartdata_update_interval = getattr(self.trader, 'chartdata_update_interval', 10)
                        update_interval = chartdata_update_interval * 1000  # 초 -> 밀리초 변환
                        self.update_timer.start(update_interval)
                        self.logger.debug(f"✅ update_timer 시작: 첫 번째 모니터링 종목 추가 (차트 데이터 업데이트: {update_interval//1000}초 간격)")
                
                # API 요청 큐에 추가
                self._add_to_api_queue(code)
            else:
                self.logger.debug(f"ℹ️ 모니터링 종목이 이미 존재함: {code}")
                
        except Exception as ex:
            self.logger.error(f"❌ 모니터링 종목 추가 실패 ({code}): {ex}", exc_info=True)
    
    def _add_to_api_queue(self, code):
        """API 요청 큐에 종목 추가"""
        try:
            if code not in self.api_request_queue:
                self.api_request_queue.append(code)
                
                # 종목명이 pending_stocks에 없으면 기본값 저장 (API 호출 제거)
                if code not in self.pending_stocks:
                    self.pending_stocks[code] = f"종목{code}"
                
                self.logger.debug(f"📋 API 요청 큐에 추가: {code} (대기 중: {len(self.api_request_queue)}개)")
            else:
                self.logger.debug(f"📋 API 요청 큐에 이미 존재: {code}")
        except Exception as ex:
            self.logger.error(f"❌ API 큐 추가 실패 ({code}): {ex}")
    
    def _process_api_queue(self):
        """API 요청 큐 처리 (1초 간격)"""
        try:
            if not self.api_request_queue or self.queue_processing:
                return
            
            # 큐 처리 시작
            self.queue_processing = True
            
            # 큐에서 첫 번째 종목 가져오기
            code = self.api_request_queue.pop(0)
            name = self.pending_stocks.get(code)  # 종목명 가져오기
            
            self.logger.debug(f"🔧 큐에서 데이터 수집 시작: {code} (남은 큐: {len(self.api_request_queue)}개)")
            
            # 차트 데이터 수집 (QThread에서 비동기 실행)
            self.update_single_chart(code)
            
        except Exception as ex:
            self.logger.error(f"❌ API 큐 처리 실패: {ex}")
        finally:
            # 큐 처리 완료
            self.queue_processing = False

    def remove_monitoring_stock(self, code):
        """모니터링 종목 제거"""
        if code in self.cache:
            del self.cache[code]
            self.logger.debug(f"📊 모니터링 종목 제거: {code}")
    
    def update_monitoring_stocks(self, codes):
        """모니터링 종목 리스트 업데이트"""
        try:
            self.logger.debug(f"🔧 모니터링 종목 리스트 업데이트 시작")
            self.logger.debug(f"새로운 종목 리스트: {codes}")
            
            current_codes = set(self.cache.keys())
            new_codes = set(codes)
            
            self.logger.debug(f"현재 캐시된 종목: {list(current_codes)}")
            self.logger.debug(f"새로운 종목: {list(new_codes)}")
            
            # 추가할 종목 (순차적으로 처리)
            to_add = new_codes - current_codes
            if to_add:
                self.logger.debug(f"추가할 종목: {list(to_add)}")
                self._add_monitoring_stocks_sequentially(list(to_add))
            
            # 제거할 종목
            to_remove = current_codes - new_codes
            if to_remove:
                self.logger.debug(f"제거할 종목: {list(to_remove)}")
                for code in to_remove:
                    self.remove_monitoring_stock(code)
            
            # 모니터링 종목 변경 로그
            if new_codes:
                logging.debug(f"✅ 모니터링 종목 변경 완료: {list(new_codes)}")
            else:
                logging.warning("⚠️ 모니터링 종목이 없습니다")
                
        except Exception as ex:
            self.logger.error(f"❌ 모니터링 종목 리스트 업데이트 실패: {ex}", exc_info=True)
    
    def _start_queue_processing(self):
        """API 큐 처리 시작"""
        try:
            if self.queue_timer:
                return  # 이미 처리 중
            
            self.queue_timer = QTimer()
            self.queue_timer.timeout.connect(self._process_api_queue)
            self.queue_timer.start(3000)  # 3초 간격으로 처리
            
        except Exception as ex:
            self.logger.error(f"❌ 큐 처리 시작 실패: {ex}")
    
    def _add_monitoring_stocks_sequentially(self, codes):
        """모니터링 종목을 큐에 추가 (API 제한 고려)"""
        if not codes:
            return
        
        logging.debug(f"📋 {len(codes)}개 종목을 API 큐에 추가: {codes}")
        
        # 모든 종목을 큐에 추가 (중복 제거)
        for code in codes:
            if code not in self.api_request_queue:
                self.api_request_queue.append(code)
                
                # 종목코드만 저장 (API 호출 제거)
                self.pending_stocks[code] = f"종목{code}"
                
                logging.debug(f"📋 API 요청 큐에 추가: {code}")
        
        logging.debug(f"✅ 총 {len(self.api_request_queue)}개 종목이 큐에 대기 중")
    
    def update_single_chart(self, code):
        """단일 종목 차트 데이터 업데이트 (비동기)"""
        try:
            logging.debug(f"🔧 차트 데이터 업데이트 시작: {code}")
            
            # 트레이더 객체 확인
            if not hasattr(self, 'trader') or not self.trader:
                logging.warning(f"⚠️ 트레이더 객체가 없음: {code} (API 연결을 확인해주세요)")
                return
            
            if not hasattr(self.trader, 'client') or not self.trader.client:
                logging.warning(f"⚠️ 트레이더 클라이언트가 없음: {code}")
                return
            
            if not self.trader.client.is_connected:
                logging.warning(f"⚠️ API 연결되지 않음: {code}")
                return

            # 비동기 차트 데이터 수집 (UI 블로킹 방지)
            self.collect_chart_data_async(code)
            # finally 블록에서 queue_processing을 False로 설정하는 로직을 _collect_chart_data_internal로 이동
            
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 업데이트 실패: {code} - {ex}")
    
    def update_all_charts(self):
        """모든 모니터링 종목 차트 데이터 업데이트 - 큐 시스템 사용"""
        try:
            self.logger.info("🔄 주기적 차트 데이터 업데이트 실행 (모든 모니터링 종목)")

            now = datetime.now()
            
            # 장 시작 시간(09:00) 이전에는 업데이트 중지
            market_open_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now < market_open_time:
                logging.debug(f"⏰ 장 시작 시간({market_open_time.strftime('%H:%M:%S')}) 이전이므로 전체 차트 데이터 업데이트를 중지합니다.")
                return
                
            # 장 마감 시간(15:30) 이후에는 업데이트 중지
            market_close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
            
            if now > market_close_time:
                logging.debug(f"⏰ 장 마감 시간({market_close_time.strftime('%H:%M:%S')}) 이후이므로 전체 차트 데이터 업데이트를 중지합니다.")
                return
            
            # UI의 모니터링 리스트 박스에서 직접 종목 코드를 가져옴
            monitoring_codes = self.parent.monitoring_manager.get_monitoring_stock_codes()
            logging.debug(f"🔧 전체 차트 데이터 업데이트 시작 - 모니터링 종목: {monitoring_codes}")
            
            if not monitoring_codes:
                logging.debug("⚠️ 모니터링 중인 종목이 없어 주기적 업데이트를 건너뜁니다.")
                return
            
            # 모든 모니터링 종목을 API 요청 큐에 즉시 추가 (중복 확인 없이)
            added_count = 0
            for code in monitoring_codes:
                self.api_request_queue.append(code)
                added_count += 1
            
            self.logger.info(f"📋 주기적 업데이트: {added_count}개 종목을 API 요청 큐에 추가 (총 큐: {len(self.api_request_queue)}개)")
            
        except Exception as ex:
            logging.error(f"❌ 전체 차트 데이터 업데이트 실패: {ex}", exc_info=True)
    
    def add_stock_to_api_queue(self, code):
        """종목을 API 큐에 추가 (차트 데이터 수집 후 모니터링에 추가)"""
        try:
            # 이미 모니터링에 존재하는지 확인
            if hasattr(self, 'parent') and self.parent and hasattr(self.parent.trading_tab, 'monitoringBox'):
                for i in range(self.parent.trading_tab.monitoringBox.count()):
                    existing_code = self.parent.trading_tab.monitoringBox.item(i).text()
                    if existing_code == code:
                        self.logger.debug(f"종목이 이미 모니터링에 존재합니다: {code}")
                        return False
            
            # API 큐에 추가 (중복 제거)
            if code not in self.api_request_queue:
                self.api_request_queue.append(code)
                if code not in self.pending_stocks:
                    self.pending_stocks[code] = f"종목{code}"
                return True
            else:
                self.logger.debug(f"종목이 이미 API 큐에 존재합니다: {code}")
                return True
        except Exception as ex:
            return False

    def get_chart_data(self, code):
        """캐시된 차트 데이터 조회"""
        try:
            cached_data = self.cache.get(code, None)
            if cached_data:
                tic_data = cached_data.get('tic_data')
                min_data = cached_data.get('min_data')
                if tic_data and min_data:
                    tic_count = len(tic_data.get('close', []))
                    min_count = len(min_data.get('close', []))
                    logging.debug(f"📊 ChartDataCache에서 {code} 데이터 조회 성공 - 틱:{tic_count}개, 분봉:{min_count}개")
                    return cached_data
                else:
                    logging.debug(f"📊 ChartDataCache에 {code} 데이터가 있지만 틱/분봉 데이터가 없음")
                    # 상세 디버깅 정보 추가
                    logging.debug(f"📊 {code} 캐시 상세: {cached_data.keys()}")
                    
                    # tic_data와 min_data의 실제 값 확인
                    logging.debug(f"📊 {code} tic_data 타입: {type(tic_data)}, 값: {tic_data}")
                    logging.debug(f"📊 {code} min_data 타입: {type(min_data)}, 값: {min_data}")
                    
                    if tic_data and isinstance(tic_data, dict):
                        logging.debug(f"📊 {code} 틱데이터 키: {tic_data.keys()}")
                        if 'close' in tic_data:
                            logging.debug(f"📊 {code} 틱데이터 close 길이: {len(tic_data.get('close', []))}")
                    if min_data and isinstance(min_data, dict):
                        logging.debug(f"📊 {code} 분봉데이터 키: {min_data.keys()}")
                        if 'close' in min_data:
                            logging.debug(f"📊 {code} 분봉데이터 close 길이: {len(min_data.get('close', []))}")
                    return None
            else:
                logging.debug(f"📊 ChartDataCache에 {code} 데이터가 없음")
                # 현재 캐시된 모든 종목 출력
                cache_keys = list(self.cache.keys())
                logging.debug(f"📊 현재 캐시된 종목들: {cache_keys}")
                return None
        except Exception as ex:
            logging.error(f"❌ 캐시 데이터 조회 실패: {code} - {ex}")
            return None
    
    def save_chart_data(self, code, tic_data, min_data):
        """차트 데이터를 캐시에 저장"""
        try:
            # 기존 캐시의 previous_close 값 유지
            previous_close = self.cache.get(code, {}).get('previous_close', 0)
            
            self.cache[code] = {
                'tic_data': tic_data,
                'min_data': min_data,
                'last_update': datetime.now(),
                'last_save': None,
                'previous_close': previous_close  # 전일종가 유지
            }
            
            tic_count = len(tic_data.get('close', [])) if tic_data else 0
            min_count = len(min_data.get('close', [])) if min_data else 0
            
            logging.debug(f"📊 ChartDataCache에 {code} 데이터 저장 완료 - 틱:{tic_count}개, 분봉:{min_count}개")
            return True
            
        except Exception as ex:
            logging.error(f"ChartDataCache 데이터 저장 실패 ({code}): {ex}")
            return False
    
    def get_tic_data_from_api(self, code, max_retries=3):
        """60틱봉 데이터 조회 (재시도 로직 포함)"""
        
        for attempt in range(max_retries):
            try:
                # API 요청 간격 조정 (첫 번째 시도가 아닌 경우 대기)
                if attempt > 0:
                    wait_time = 2 ** attempt  # 지수 백오프: 2초, 4초, 8초
                    logging.debug(f"⏳ API 제한 대기 중... ({wait_time}초 후 재시도 {attempt + 1}/{max_retries})")
                    # QTimer를 사용한 비동기 대기 (UI 블로킹 방지)
                    QTimer.singleShot(int(wait_time * 1000), lambda: None)
                
                logging.debug(f"🔧 API 틱 데이터 조회 시작: {code} (시도 {attempt + 1}/{max_retries})")
                # 동기 메서드에서 비동기 호출을 위해 run_until_complete 사용
                try:
                    loop = asyncio.get_event_loop()
                    data = loop.run_until_complete(self.trader.client.get_stock_tic_chart(code, tic_scope=30))
                except RuntimeError:
                    # 이벤트 루프가 없으면 새로 생성
                    data = asyncio.run(self.trader.client.get_stock_tic_chart(code, tic_scope=30))
                
                # API 응답 상세 로깅
                if data:
                    logging.debug(f"📊 {code} API 틱 데이터 키: {data.keys() if isinstance(data, dict) else 'dict가 아님'}")
                    if isinstance(data, dict) and 'close' in data:
                        logging.debug(f"📊 {code} API 틱 데이터 close 길이: {len(data.get('close', []))}")
                
                if not data:
                    logging.warning(f"⚠️ 틱 데이터가 None: {code}")
                    if attempt < max_retries - 1:
                        continue
                    return None
                    
                close_data = data.get('close', [])
                if len(close_data) == 0:
                    logging.warning(f"⚠️ 틱 데이터가 비어있음: {code}")
                    if attempt < max_retries - 1:
                        continue
                    return None
                    
                logging.debug(f"✅ 틱 데이터 조회 성공: {code} - 데이터 개수: {len(close_data)}")
                return data
                
            except Exception as ex:
                error_msg = str(ex)
                if "429" in error_msg or "허용된 요청 개수를 초과" in error_msg:
                    logging.warning(f"⚠️ API 제한으로 인한 틱 데이터 조회 실패 ({code}): {ex}")
                    if attempt < max_retries - 1:
                        logging.debug(f"💡 재시도 예정 ({attempt + 1}/{max_retries})")
                        continue
                    else:
                        logging.error(f"❌ 최대 재시도 횟수 초과: {code}")
                        return None
                else:
                    logging.error(f"❌ 틱 데이터 조회 실패 ({code}): {ex}", exc_info=True)
                    return None
        
        return None
    
    def get_min_data_from_api(self, code, max_retries=3):
        """3분봉 데이터 조회 (재시도 로직 포함)"""
        
        for attempt in range(max_retries):
            try:
                # API 요청 간격 조정 (첫 번째 시도가 아닌 경우 대기)
                if attempt > 0:
                    wait_time = 2 ** attempt  # 지수 백오프: 2초, 4초, 8초
                    logging.debug(f"⏳ API 제한 대기 중... ({wait_time}초 후 재시도 {attempt + 1}/{max_retries})")
                    # QTimer를 사용한 비동기 대기 (UI 블로킹 방지)
                    QTimer.singleShot(int(wait_time * 1000), lambda: None)
                
                logging.debug(f"🔧 API 분봉 데이터 조회 시작: {code} (시도 {attempt + 1}/{max_retries})")
                # 동기 메서드에서 비동기 호출을 위해 run_until_complete 사용
                try:
                    loop = asyncio.get_event_loop()
                    data = loop.run_until_complete(self.trader.client.get_stock_minute_chart(code, period=3))
                except RuntimeError:
                    # 이벤트 루프가 없으면 새로 생성
                    data = asyncio.run(self.trader.client.get_stock_minute_chart(code, period=3))
                
                # API 응답 상세 로깅
                logging.debug(f"📊 {code} API 분봉 데이터 응답 타입: {type(data)}")
                if data:
                    logging.debug(f"📊 {code} API 분봉 데이터 키: {data.keys() if isinstance(data, dict) else 'dict가 아님'}")
                    if isinstance(data, dict) and 'close' in data:
                        logging.debug(f"📊 {code} API 분봉 데이터 close 길이: {len(data.get('close', []))}")
                
                if not data:
                    logging.warning(f"⚠️ 분봉 데이터가 None: {code}")
                    if attempt < max_retries - 1:
                        continue
                    return None
                    
                close_data = data.get('close', [])
                if len(close_data) == 0:
                    logging.warning(f"⚠️ 분봉 데이터가 비어있음: {code}")
                    if attempt < max_retries - 1:
                        continue
                    return None
                    
                logging.debug(f"✅ 분봉 데이터 조회 성공: {code} - 데이터 개수: {len(close_data)}")
                return data
                
            except Exception as ex:
                error_msg = str(ex)
                if "429" in error_msg or "허용된 요청 개수를 초과" in error_msg:
                    logging.warning(f"⚠️ API 제한으로 인한 분봉 데이터 조회 실패 ({code}): {ex}")
                    if attempt < max_retries - 1:
                        logging.debug(f"💡 재시도 예정 ({attempt + 1}/{max_retries})")
                        continue
                    else:
                        logging.error(f"❌ 최대 재시도 횟수 초과: {code}")
                        return None
                else:
                    logging.error(f"❌ 분봉 데이터 조회 실패 ({code}): {ex}", exc_info=True)
                    return None
        
        return None
    
    def _trigger_async_save_to_database(self):
        """비동기 데이터베이스 저장 트리거"""
        try:
            # qasync 환경에서 메인 이벤트 루프 사용 시도
            try:
                loop = asyncio.get_running_loop()
                # 이미 실행 중인 이벤트 루프가 있으면 태스크로 실행
                asyncio.create_task(self.save_to_database())
                logging.debug("✅ DB 저장을 비동기 태스크로 시작")
                return
            except RuntimeError:
                # 실행 중인 이벤트 루프가 없음 - ThreadPoolExecutor로 처리
                logging.debug("⚠️ 실행 중인 이벤트 루프가 없어 ThreadPoolExecutor 사용")
                pass
            
            def run_async_save():
                try:
                    # 새로운 이벤트 루프 생성
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        # 비동기 저장 실행
                        return loop.run_until_complete(self.save_to_database())
                    finally:
                        loop.close()
                except Exception as e:
                    logging.error(f"비동기 데이터베이스 저장 실행 오류: {e}")
                    return None
            
            # 별도 스레드에서 비동기 저장 실행
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_async_save)
                future.result(timeout=30)  # 30초 타임아웃
                
        except Exception as ex:
            logging.error(f"비동기 데이터베이스 저장 트리거 실패: {ex}")

    async def save_to_database(self):
        """차트 데이터를 DB에 저장 (비동기 I/O)"""
        try:
            now = datetime.now()
            
            # 장 시작 시간(09:00) 이전에는 DB 저장 중지
            market_open_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now < market_open_time:
                logging.debug(f"⏰ 장 시작 시간({market_open_time.strftime('%H:%M:%S')}) 이전이므로 DB 저장을 중지합니다.")
                return
                
            # 장 마감 시간(15:30) 이후에는 DB 저장 중지
            market_close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
            
            if now > market_close_time:
                logging.debug(f"⏰ 장 마감 시간({market_close_time.strftime('%H:%M:%S')}) 이후이므로 DB 저장을 중지합니다.")
                return

            if not hasattr(self.trader, 'db_manager') or not self.trader.db_manager:
                logging.warning("❌ DB 매니저가 없어서 저장할 수 없습니다")
                return
            
            current_time = datetime.now()
            saved_count = 0
            cache_count = len(self.cache)
            
            logging.debug(f"🔍 캐시 상태 확인: {cache_count}개 종목")
            
            for code in list(self.cache.keys()):
                data = self.cache.get(code)
                if not data: continue

                original_tic_data = data.get('tic_data')
                original_min_data = data.get('min_data')

                # DataFrame으로 변환하여 지표 계산
                try:
                    tic_df = pd.DataFrame(original_tic_data) if original_tic_data else pd.DataFrame()
                    min_df = pd.DataFrame(original_min_data) if original_min_data else pd.DataFrame()

                    # extract_chart_indicators를 사용하여 모든 지표 계산
                    tic_indicators = strategy_utils.KiwoomIndicatorExtractor.extract_chart_indicators(tic_df)
                    min_indicators = strategy_utils.KiwoomIndicatorExtractor.extract_chart_indicators(min_df)

                    # 계산된 지표를 DataFrame에 다시 병합
                    # 기본 컬럼(open, high, low, close, volume)은 덮어쓰지 않음
                    base_cols = {'open', 'high', 'low', 'close', 'volume'}
                    for key, value in tic_indicators.items():
                        if key not in base_cols: tic_df[key] = value
                    for key, value in min_indicators.items(): min_df[key] = value
                except Exception as df_ex:
                    self.logger.error(f"DB 저장용 데이터프레임 변환/지표 계산 실패 ({code}): {df_ex}")
                    continue
                
                logging.debug(f"🔍 {code}: tic_data={not tic_df.empty}, min_data={not min_df.empty}")
                
                if tic_df.empty or min_df.empty:
                    logging.warning(f"⚠️ {code}: 데이터 부족으로 저장 건너뜀 (tic: {not tic_df.empty}, min: {not min_df.empty})")
                    continue
                
                # 1분마다 저장 (마지막 저장 시간 확인)
                last_save = data.get('last_save')
                if last_save:
                    time_diff = (current_time - last_save).total_seconds()
                    if time_diff < 59:  # 59초 미만일 때만 건너뜀 (60초 타이밍 이슈 방지)
                        logging.debug(f"⏰ {code}: 아직 저장 시간이 안 됨 (경과: {time_diff:.1f}초, 마지막 저장: {last_save})")
                        continue
                
                logging.debug(f"💾 {code}: DB 저장 시작")
                
                # 통합 주식 데이터 저장 (틱봉 기준, 분봉 데이터 포함)
                await self.trader.db_manager.save_stock_data(code, tic_df.to_dict('list'), min_df.to_dict('list'))
                
                # 저장 시간 업데이트
                data['last_save'] = current_time
                saved_count += 1
                
                logging.debug(f"✅ {code}: DB 저장 완료")
            
            if saved_count > 0:
                logging.debug(f"📊 통합 차트 데이터 DB 저장 완료: {saved_count}개 종목")
            else:
                logging.warning("⚠️ 저장된 데이터가 없습니다")
                
        except Exception as ex:
            logging.error(f"통합 차트 데이터 DB 저장 실패: {ex}", exc_info=True)
    
    def log_single_stock_analysis(self, code, tic_data, min_data):
        """단일 종목 분석표 출력 (차트 데이터 저장 시) - 비활성화됨"""
        try:
            # 종목명 조회
            stock_name = self.get_stock_name(code)
            
            # 분석표 출력 비활성화 - 간단한 로그만 출력
            logging.debug(f"📊 {stock_name}({code}) 차트 데이터 저장 완료")            
            
        except Exception as ex:
            logging.error(f"단일 종목 분석표 출력 실패 ({code}): {ex}")
    
    def log_all_monitoring_analysis(self):
        """모든 모니터링 종목에 대한 분석표 출력 - 비활성화됨"""
        try:
            if not self.cache:
                return
            
            # 분석표 출력 비활성화 - 간단한 로그만 출력
            logging.debug(f"📊 모든 모니터링 종목 분석표 완료 - 캐시된 종목: {len(self.cache)}개")
                       
        except Exception as ex:
            logging.error(f"모니터링 종목 분석표 출력 실패: {ex}")
    
    def get_stock_name(self, code):
        """종목코드로 종목명 조회 (API 호출 제거)"""
        # API 제한 초과 방지를 위해 종목코드만 반환
        return f"종목{code}"
    
    def start(self):
        """캐시 업데이트 및 저장 타이머 시작"""
        try:
            if self.update_timer and not self.update_timer.isActive():
                chartdata_update_interval = getattr(self.trader, 'chartdata_update_interval', 10)
                self.update_timer.start(chartdata_update_interval * 1000)
            if self.save_timer and not self.save_timer.isActive():
                self.save_timer.start(60000)
            self.logger.debug("✅ ChartDataCache 타이머 시작")
        except Exception as ex:
            self.logger.error(f"❌ ChartDataCache 타이머 시작 실패: {ex}", exc_info=True)


    def stop(self):
        """캐시 정리"""
        try:
            if self.update_timer:
                self.update_timer.stop()
            if self.save_timer:
                self.save_timer.stop()
            self.cache.clear()
            logging.debug("📊 차트 데이터 캐시 정리 완료")
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 캐시 정리 실패: {ex}", exc_info=True)
    
    def _calculate_technical_indicators(self, data, chart_type=None):
        """기술적 지표 계산"""
        try:
            if not data or not isinstance(data, dict):
                return data
                
            close_prices = data.get('close', [])
            high_prices = data.get('high', [])
            low_prices = data.get('low', [])
            volumes = data.get('volume', [])
            
            if len(close_prices) < 5:
                return data
                
            
            # numpy 배열로 변환
            close_array = np.array(close_prices, dtype=float)
            high_array = np.array(high_prices, dtype=float)
            low_array = np.array(low_prices, dtype=float)
            volume_array = np.array(volumes, dtype=float)
            
            indicators = {}
            
            # 차트 유형별 이동평균선 계산
            if chart_type == "tic":
                # 틱 차트: MA5, MA20, MA60, MA120
                if len(close_array) >= 5:
                    indicators['MA5'] = talib.SMA(close_array, timeperiod=5)
                if len(close_array) >= 20:
                    indicators['MA20'] = talib.SMA(close_array, timeperiod=20)
                if len(close_array) >= 60:
                    indicators['MA60'] = talib.SMA(close_array, timeperiod=60)
                if len(close_array) >= 120:
                    indicators['MA120'] = talib.SMA(close_array, timeperiod=120)
            elif chart_type == "minute":
                # 3분봉 차트: MA5, MA10, MA20
                if len(close_array) >= 5:
                    indicators['MA5'] = talib.SMA(close_array, timeperiod=5)
                if len(close_array) >= 10:
                    indicators['MA10'] = talib.SMA(close_array, timeperiod=10)
                if len(close_array) >= 20:
                    indicators['MA20'] = talib.SMA(close_array, timeperiod=20)
            else:
                # 기본값: 모든 이동평균선 계산 (기존 로직)
                if len(close_array) >= 5:
                    indicators['MA5'] = talib.SMA(close_array, timeperiod=5)
                if len(close_array) >= 10:
                    indicators['MA10'] = talib.SMA(close_array, timeperiod=10)
                if len(close_array) >= 20:
                    indicators['MA20'] = talib.SMA(close_array, timeperiod=20)
                if len(close_array) >= 50:
                    indicators['MA50'] = talib.SMA(close_array, timeperiod=50)
                if len(close_array) >= 60:
                    indicators['MA60'] = talib.SMA(close_array, timeperiod=60)
                if len(close_array) >= 120:
                    indicators['MA120'] = talib.SMA(close_array, timeperiod=120)
                
            # RSI 계산
            if len(close_array) >= 14:
                indicators['RSI'] = talib.RSI(close_array, timeperiod=14)
                
            # MACD 계산
            if len(close_array) >= 26:
                macd, macd_signal, macd_hist = talib.MACD(close_array)
                indicators['MACD'] = macd
                indicators['MACD_SIGNAL'] = macd_signal
                indicators['MACD_HIST'] = macd_hist
                
            # 볼린저 밴드
            if len(close_array) >= 20:
                bb_upper, bb_middle, bb_lower = talib.BBANDS(close_array, timeperiod=20)
                indicators['BB_UPPER'] = bb_upper
                indicators['BB_MIDDLE'] = bb_middle
                indicators['BB_LOWER'] = bb_lower
                
            # 스토캐스틱
            if len(high_array) >= 14 and len(low_array) >= 14:
                slowk, slowd = talib.STOCH(high_array, low_array, close_array)
                indicators['STOCH_K'] = slowk
                indicators['STOCH_D'] = slowd
                
            # Williams %R
            if len(high_array) >= 14 and len(low_array) >= 14:
                williams_r = talib.WILLR(high_array, low_array, close_array, timeperiod=14)
                indicators['WILLIAMS_R'] = williams_r
                
            # ROC (Rate of Change)
            if len(close_array) >= 10:
                roc = talib.ROC(close_array, timeperiod=10)
                indicators['ROC'] = roc
                
            # OBV (On Balance Volume)
            if len(close_array) >= 1 and len(volume_array) >= 1:
                obv = talib.OBV(close_array, volume_array)
                indicators['OBV'] = obv
                
                # OBV의 20일 이동평균
                if len(obv) >= 20:
                    obv_ma20 = talib.SMA(obv, timeperiod=20)
                    indicators['OBV_MA20'] = obv_ma20
                
            # ATR (Average True Range)
            if len(high_array) >= 14 and len(low_array) >= 14:
                atr = talib.ATR(high_array, low_array, close_array, timeperiod=14)
                indicators['ATR'] = atr
                
            # 데이터에 지표 직접 추가
            for key, value in indicators.items():
                data[key] = value
            
            return data
            
        except Exception as ex:
            logging.error(f"❌ 기술적 지표 계산 실패: {ex}")
            return data
    
    def get_cached_data(self, code):
        """특정 종목의 캐시된 데이터 반환"""
        try:
            if code in self.cache:
                return self.cache[code]
            return None
        except Exception as ex:
            logging.error(f"❌ 캐시 데이터 조회 실패: {code} - {ex}")
            return None
    
    def update_realtime_chart_data(self, code, tic_data, min_data):
        """실시간 차트 데이터 업데이트"""
        try:
            if code not in self.cache:
                self.cache[code] = {}
            
            # 기존 데이터와 실시간 데이터 병합
            if 'tic_data' in self.cache[code] and tic_data:
                # 틱 데이터 병합
                existing_tic = self.cache[code]['tic_data']
                for key in ['time', 'open', 'high', 'low', 'close', 'volume', 'strength', 'MA5', 'MA10', 'MA20', 'MA50', 'EMA5', 'EMA10', 'EMA20', 'RSI', 'MACD', 'MACD_SIGNAL', 'MACD_HIST']:
                    if key in tic_data and key in existing_tic:
                        existing_tic[key].extend(tic_data[key])
                        # 최대 데이터 수 제한
                        if len(existing_tic[key]) > 300:
                            existing_tic[key] = existing_tic[key][-300:]
                self.cache[code]['tic_data'] = existing_tic
            
            if 'min_data' in self.cache[code] and min_data:
                # 분봉 데이터 병합
                existing_min = self.cache[code]['min_data']
                for key in ['time', 'open', 'high', 'low', 'close', 'volume', 'MA5', 'MA10', 'MA20', 'MA50', 'EMA5', 'EMA10', 'EMA20', 'RSI', 'MACD', 'MACD_SIGNAL', 'MACD_HIST']:
                    if key in min_data and key in existing_min:
                        existing_min[key].extend(min_data[key])
                        # 최대 데이터 수 제한
                        if len(existing_min[key]) > 150:
                            existing_min[key] = existing_min[key][-150:]
                self.cache[code]['min_data'] = existing_min
            
            self.cache[code]['last_updated'] = datetime.now()
            
            # 실시간 차트 업데이트 시그널 발생
            self.data_updated.emit(code)
            
        except Exception as ex:
            logging.error(f"실시간 차트 데이터 업데이트 실패 ({code}): {ex}")

    async def add_realtime_data_async(self, stock_code, realtime_data):
        """실시간 데이터를 차트 데이터에 추가하고 지표를 비동기적으로 계산"""
        try:
            # MyWindow의 chart_cache에 접근
            if not hasattr(self, 'parent') or not self.parent:
                return

            chart_cache = self.parent.chart_cache

            # 차트 캐시에서 기존 데이터 가져오기
            cached_data = chart_cache.get_cached_data(stock_code)

            if not cached_data or not isinstance(cached_data, dict):
                if not hasattr(self, '_no_cache_logged'):
                    self._no_cache_logged = set()
                if stock_code not in self._no_cache_logged:
                    self.logger.debug(f"ℹ️ 실시간 데이터 수신({stock_code}), 차트 캐시 생성 대기 중...")
                    self._no_cache_logged.add(stock_code)
                return

            tic_data = cached_data.get('tic_data')
            min_data = cached_data.get('min_data')

            if not tic_data or not isinstance(tic_data, dict) or not min_data or not isinstance(min_data, dict):
                self.logger.debug(f"⚠️ 차트 데이터 추가 건너뜀: {stock_code} (데이터 없음 또는 잘못된 타입)")
                return

            # 실시간 데이터를 틱/분봉 데이터에 추가
            self.parent.login_handler.websocket_client._update_tic_chart_with_realtime(stock_code, cached_data, realtime_data)
            self.parent.login_handler.websocket_client._update_minute_chart_with_realtime(stock_code, cached_data, realtime_data)

            # 차트 캐시 업데이트
            chart_cache.cache[stock_code] = cached_data

            # 실시간 기술적 지표 계산 (비동기, ThreadPoolExecutor 사용)
            loop = asyncio.get_running_loop()
            tasks = []
            if tic_data:
                tasks.append(loop.run_in_executor(None, chart_cache._calculate_technical_indicators, tic_data, "tic"))
            if min_data:
                tasks.append(loop.run_in_executor(None, chart_cache._calculate_technical_indicators, min_data, "minute"))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # 결과 처리
                if tic_data and not isinstance(results[0], Exception):
                    cached_data['tic_data'] = results[0]
                elif tic_data:
                    logging.error(f"틱 지표 계산 실패: {results[0]}")

                if min_data and len(results) > 1 and not isinstance(results[1], Exception):
                    cached_data['min_data'] = results[1]
                elif min_data and len(results) > 1:
                    logging.error(f"분봉 지표 계산 실패: {results[1]}")

                chart_cache.cache[stock_code] = cached_data

            # 데이터 업데이트 시그널 발생
            self.data_updated.emit(stock_code)

        except Exception as e:
            self.logger.error(f"실시간 차트 데이터 추가 실패: {e}", exc_info=True)

class KiwoomWebSocketClient:
    """키움 웹소켓 클라이언트 (asyncio 기반) - 리팩토링된 버전"""
    
    def __init__(self, token: str, logger: logging.Logger, is_mock: bool = False, parent=None):
        # 키움증권 예시코드에 맞춰 URL 설정
        if is_mock:
            self.uri = 'wss://mockapi.kiwoom.com:10000/api/dostk/websocket'  # 모의투자 웹소켓 URL
        else:
            self.uri = 'wss://api.kiwoom.com:10000/api/dostk/websocket'  # 실제투자 웹소켓 URL
        self.logger = logging.getLogger(self.__class__.__name__)
        
        self.token = token
        self.logger = logger
        self.is_mock = is_mock
        self.websocket = None
        self.connected = False
        self.keep_running = True
        self.subscribed_codes = set()
        self.message_queue = queue.Queue()
        self.balance_data = {}  # 잔고 데이터 저장
        self.market_status = {}  # 시장 상태 데이터 저장
        self._connecting = False  # 중복 연결 방지 플래그
        self._connection_lock = asyncio.Lock()  # 연결 락
        self.parent = parent  # 부모 윈도우 참조
        self._last_table_update_time = 0  # 마지막 투자현황표 업데이트 시간
        self._table_update_interval = 1.0  # 투자현황표 업데이트 최소 간격(초)
        self._pending_subscriptions = {}  # 타입별 그룹 번호 추적 {type: grp_no}
        
    async def connect(self):
        """웹소켓 연결 (키움증권 예시코드 기반)"""
        try:
            mode_text = "모의투자" if self.is_mock else "실제투자" # type: ignore
            logging.debug(f"🔧 웹소켓 연결 시작... ({mode_text})")
            
            # 웹소켓 연결 (키움증권 예시코드와 동일)
            self.websocket = await websockets.connect(self.uri, ping_interval=None)
            self.connected = True
            
            # 로그인 패킷 (키움증권 예시코드 구조)
            login_param = {
                'trnm': 'LOGIN',
                'token': self.token
            }
            
            logging.debug('🔧 실시간 체결 서버로 로그인 패킷을 전송합니다.')
            # 웹소켓 연결 시 로그인 정보 전달
            await self.send_message(login_param)
            
            return True
            
        except Exception as e:
            self.logger.error(f'웹소켓 연결 오류: {e}', exc_info=True)
            self.connected = False
            return False
    
    async def disconnect(self):
        """웹소켓 연결 해제 (키움증권 예시코드 기반)"""
        try:
            self.keep_running = False
            self.connected = False

            if self.websocket:
                # 웹소켓을 닫아 recv() 대기를 중단시킵니다.
                # ConnectionClosed 예외가 발생하여 receive_messages 루프가 종료됩니다.
                if self.websocket.close_code is None:
                    await self.websocket.close()
                    self.logger.debug('✅ 웹소켓 연결을 닫았습니다.')
                self.websocket = None # type: ignore
            
            # 구독된 종목 목록 초기화
            self.subscribed_codes.clear()
            
            # 메시지 큐 정리
            while not self.message_queue.empty():
                try:
                    self.message_queue.get_nowait()
                except:
                    break
            
            # 데이터 초기화
            self.balance_data.clear()
            self.market_status.clear()
            
            self.logger.debug('✅ 웹소켓 클라이언트 완전 정리 완료')
            
        except Exception as ex:
            self.logger.error(f"웹소켓 연결 해제 실패: {ex}", exc_info=True)
            
    
    async def run(self):
        """웹소켓 클라이언트 실행 (키움증권 예시코드 기반)"""
        try:
            # 서버에 연결
            if await self.connect():
                # 메시지를 계속 받을 준비
                await self.receive_messages()

        except asyncio.CancelledError:
            self.logger.debug("🛑 웹소켓 클라이언트 태스크가 취소되었습니다")
            raise  # CancelledError는 다시 발생시켜야 함
        except websockets.ConnectionClosed:
            self.logger.info("🔌 웹소켓 연결이 정상적으로 종료되었습니다 (run).")
        except Exception as e:
            self.logger.error(f"웹소켓 클라이언트 실행 중 오류: {e}", exc_info=True)

        finally:
            self.logger.debug("🔌 웹소켓 클라이언트 정리 중...")
            await self.disconnect()
            self.logger.debug("✅ 웹소켓 클라이언트 정리 완료")

    async def send_message(self, message):
        """메시지 전송 (키움증권 예시코드 기반)"""
        if not self.connected:
            await self.connect()  # 연결이 끊어졌다면 재연결
        if self.connected:
            # message가 문자열이 아니면 JSON으로 직렬화
            if not isinstance(message, str):
                message = json.dumps(message)

            await self.websocket.send(message)
            
            # PING 메시지는 로그 출력하지 않음 (너무 빈번함)
            try:
                if isinstance(message, str):
                    message_dict = json.loads(message)
                else:
                    message_dict = message
                
                if message_dict.get('trnm') != 'PING':
                    self.logger.debug(f'메시지 전송: {message}')
            except (json.JSONDecodeError, AttributeError):
                # JSON 파싱 실패시 기본 로그 출력
                self.logger.debug(f'메시지 전송: {message}')

    async def receive_messages(self):
        """서버에서 메시지 수신"""
        self.logger.debug("🔧 웹소켓 메시지 수신 루프 시작")
        message_count = 0
        
        while self.keep_running and self.connected:
            try:
                # 수동 매매 작업이 진행 중이면 메시지 처리를 잠시 멈춥니다.
                if hasattr(self.parent, 'trading_lock') and self.parent.trading_lock.locked():
                    await asyncio.sleep(0.1) # 0.1초 대기 후 다시 확인
                    continue

                # 서버로부터 수신한 메시지를 JSON 형식으로 파싱
                # logging.debug(f"🔧 메시지 수신 대기 중... (수신된 메시지 수: {message_count})")
                message = await self.websocket.recv()
                message_count += 1
                # 원문 메시지 로그는 제거하여 중복 로그를 줄임

                response = json.loads(message)

                # 메시지 유형이 LOGIN일 경우 로그인 시도 결과 체크 (키움증권 예시코드 기반)
                if response.get('trnm') == 'LOGIN':
                    if response.get('return_code') != 0:
                        error_msg = response.get('return_msg', '')
                        self.logger.error(f'웹소켓 로그인 실패하였습니다. : {error_msg}')
                        
                        # 토큰 만료나 무효한 경우 토큰 갱신 후 재연결 시도
                        if 'Token' in error_msg or '토큰' in error_msg or '8005' in error_msg:
                            logging.info('🔄 토큰 만료로 인한 로그인 실패 - 토큰 갱신 후 재연결 시도')
                            
                            # REST 클라이언트를 통해 토큰 갱신 시도
                            if self.parent and hasattr(self.parent, 'login_handler'):
                                if hasattr(self.parent.login_handler, 'kiwoom_client'):
                                    try:
                                        # 토큰 갱신
                                        if await self.parent.login_handler.kiwoom_client.get_access_token():
                                            self.logger.info('✅ 토큰 갱신 성공 - 웹소켓 재연결 시도')
                                            # 새로운 토큰으로 업데이트
                                            self.token = self.parent.login_handler.kiwoom_client.access_token
                                            # keep_running을 True로 설정 (재연결을 위해)
                                            self.keep_running = True
                                            # 재연결 시도
                                            await asyncio.sleep(1)  # 1초 대기
                                            if await self.connect(): # type: ignore
                                                self.logger.info('✅ 웹소켓 재연결 성공')
                                            else:
                                                self.logger.error('❌ 웹소켓 재연결 실패')
                                                await self.disconnect()
                                        else:
                                            self.logger.error('❌ 토큰 갱신 실패')
                                            await self.disconnect()
                                    except Exception as token_err:
                                        self.logger.error(f'토큰 갱신 중 오류: {token_err}', exc_info=True)
                                        await self.disconnect()
                                else:
                                    self.logger.error('❌ REST 클라이언트를 찾을 수 없습니다')
                                    await self.disconnect()
                            else:
                                self.logger.error('❌ 부모 윈도우나 login_handler를 찾을 수 없습니다')
                                await self.disconnect()
                        else:
                            # 토큰 문제가 아닌 다른 오류인 경우
                            await self.disconnect()
                    else:
                        mode_text = "모의투자" if self.is_mock else "실제투자" # type: ignore
                        self.logger.debug(f'✅ 웹소켓 로그인 성공하였습니다. ({mode_text} 모드)')
                        
                        # 웹소켓 연결 성공 시 post_login_setup 실행
                        try:
                            # post_login_setup을 직접 await하여 순차적으로 실행
                            if hasattr(self, 'parent') and hasattr(self.parent, 'post_login_setup'):
                                await self.parent.post_login_setup() # type: ignore
                                self.logger.debug("✅ post_login_setup 실행 완료")
                        except Exception as setup_err:
                            self.logger.error(f"post_login_setup 실행 실패: {setup_err}", exc_info=True)
                        
                        # 로그인 성공 후 주문체결 실시간 구독 시작
                        try:
                            await self.subscribe_order_execution()
                            logging.debug("🔔 주문체결 실시간 모니터링 시작")
                        except Exception as order_sub_err:
                            logging.error(f"❌ 주문체결 구독 실패: {order_sub_err}")
                        
                        # 로그인 성공 후 실시간 잔고 구독 시작
                        try:
                            await self.subscribe_balance()
                            self.logger.debug("🔔 실시간 잔고 모니터링 시작")
                            
                            # 웹소켓 준비 완료 - 이전에 조회한 REST API 잔고 데이터가 있으면 투자현황표 업데이트
                            if hasattr(self, 'parent') and self.parent:
                                try:
                                    # 부모 윈도우의 임시 보유종목 데이터 확인
                                    if hasattr(self.parent, '_pending_balance_data'):
                                        self.logger.info("웹소켓 준비 완료 - 임시 저장된 잔고 데이터로 투자현황표 초기화")
                                        self.parent._initialize_balance_data_from_rest_api(self.parent._pending_balance_data)
                                        delattr(self.parent, '_pending_balance_data')
                                except Exception as table_update_err:
                                    self.logger.error(f"투자현황표 초기화 실패: {table_update_err}", exc_info=True)
                        except Exception as balance_sub_err:
                            self.logger.error(f"실시간 잔고 구독 실패: {balance_sub_err}", exc_info=True)
                        
                        # 로그인 성공 후 시장 상태 구독 시작
                        try:
                            await self.subscribe_market_status()
                            self.logger.debug("🔔 시장 상태 모니터링 시작")
                        except Exception as market_sub_err:
                            self.logger.error(f"시장 상태 구독 실패: {market_sub_err}", exc_info=True)

                # 메시지 유형이 PING일 경우 수신값 그대로 송신 (키움증권 예시코드 기반)
                if response.get('trnm') == 'PING':
                    await self.send_message(response)
                    continue  # PING은 더 이상 처리하지 않음
                    
                # CNSRLST 응답인 경우 조건검색 목록조회 결과 처리
                if response.get('trnm') == 'CNSRLST':
                    try:
                        # 응답 데이터 유효성 확인
                        if response is None:
                            self.logger.warning("⚠️ 조건검색 목록조회 응답 데이터가 None입니다")
                            continue
                        
                        if not isinstance(response, dict):
                            self.logger.warning(f"⚠️ 조건검색 목록조회 응답이 딕셔너리가 아닙니다: {type(response)}")
                            continue
                        
                        self.process_condition_search_list_response(response)
                    except Exception as condition_err:
                        self.logger.error(f"조건검색 목록조회 응답 처리 실패: {condition_err}", exc_info=True)
                        

                # 실시간 데이터 처리
                if response.get('trnm') == 'REAL':  # 실시간 데이터
                    
                    # 실시간 데이터 처리 (예외 처리 강화)
                    try:
                        data_list = response.get('data', [])
                        if not isinstance(data_list, list):
                            self.logger.warning(f"실시간 데이터가 리스트가 아닙니다: {type(data_list)}")
                            continue
                        
                        # 데이터가 비어있는 경우 로그 (디버깅용)
                        if len(data_list) == 0:
                            self.logger.debug("실시간 데이터 수신했으나 data 리스트가 비어있습니다")
                            continue
                            
                        for data_item in data_list:
                            try:
                                if not isinstance(data_item, dict):
                                    logging.warning(f"데이터 아이템이 딕셔너리가 아닙니다: {type(data_item)}")
                                    continue
                                    
                                data_type = data_item.get('type')
                                if data_type == '00':  # 주문체결
                                    self.logger.debug(f"📋 주문체결 실시간 수신: {data_item.get('values', {}).get('913', '')}")
                                    try:
                                        self.process_order_execution_data(data_item)
                                    except Exception as order_err:
                                        self.logger.error(f"주문체결 데이터 처리 실패: {order_err}", exc_info=True)
                                        
                                elif data_type == '04':  # 현물잔고
                                    try:
                                        self.process_balance_data(data_item)
                                    except Exception as balance_err:
                                        self.logger.error(f"잔고 데이터 처리 실패: {balance_err}", exc_info=True)
                                        
                                elif data_type == '0B':  # 주식체결
                                    try:
                                        # 비동기로 처리하여 기술적 지표 계산이 UI를 블로킹하지 않도록 함
                                        asyncio.create_task(self.process_stock_execution_data_async(data_item))
                                    except Exception as execution_err:
                                        self.logger.error(f"체결 데이터 처리 실패: {execution_err}", exc_info=True)
                                        
                                elif data_type == '0s':  # 시장 상태
                                    try:
                                        self.process_market_status_data(data_item)
                                    except Exception as market_err:
                                        self.logger.error(f"시장 상태 데이터 처리 실패: {market_err}", exc_info=True)
                                        
                                elif data_type == '02':  # 조건검색 실시간 알림
                                    self.logger.debug(f"조건검색 실시간 알림 수신: {data_item.get('item')}")
                                    try:
                                        self.process_condition_realtime_notification(data_item)
                                    except Exception as condition_err:
                                        self.logger.error(f"조건검색 실시간 알림 처리 실패: {condition_err}", exc_info=True)
                                        
                                else:
                                    logging.debug(f"알 수 없는 실시간 데이터 타입: {data_type}")
                            except Exception as data_item_err:
                                self.logger.error(f"실시간 데이터 아이템 처리 실패: {data_item_err}", exc_info=True)
                                
                                continue
                        
                        # 메시지 큐에 추가 (예외 처리)
                        try:
                            self.message_queue.put(response)
                        except Exception as queue_err:
                            self.logger.error(f"메시지 큐 추가 실패: {queue_err}", exc_info=True)
                            
                    except Exception as data_process_err:
                        self.logger.error(f"실시간 데이터 처리 실패: {data_process_err}", exc_info=True)
                        
                        continue
                
                # 조건검색 응답 처리 (일반 요청 및 실시간 알림)
                if response.get('trnm') == 'CNSRREQ':  # 조건검색 응답
                    try:
                        # 응답 데이터 유효성 확인
                        if response is None:
                            self.logger.warning("⚠️ 조건검색 응답 데이터가 None입니다")
                            continue
                        
                        if not isinstance(response, dict):
                            self.logger.warning(f"⚠️ 조건검색 응답이 딕셔너리가 아닙니다: {type(response)}")
                            continue
                        
                        # 조건검색 응답 데이터 전체 출력
                        data_list = response.get('data')
                        if data_list is None:
                            data_list = []
                        logging.debug("조건검색 응답 수신(CNSRREQ): return_code=%s, cont_yn=%s, count=%d",
                                          response.get('return_code'), # type: ignore
                                          response.get('cont_yn'),
                                          len(data_list))
                        
                        # 응답 타입에 따라 분기 처리
                        search_type = response.get('search_type', '0')

                        if search_type == '1':  # 실시간 요청 응답
                            self.logger.debug("조건검색 실시간 요청에 대한 초기 응답 처리")
                            self.process_condition_realtime_response(response)
                        else:
                            self.logger.debug(f"조건검색 일반 요청 응답 처리 (search_type: {search_type})")
                            self.process_condition_realtime_response(response)  # 일반 요청도 동일하게 처리
                    except Exception as condition_err:
                        self.logger.error(f"조건검색 응답 처리 실패: {condition_err}", exc_info=True)
                        

            except websockets.ConnectionClosed as e:
                self.logger.warning(f'웹소켓 연결이 서버에 의해 종료되었습니다: {e}')
                self.connected = False
                # 정상적인 종료인지 확인
                if e.code == 1000:  # 정상 종료
                    self.logger.info('웹소켓 정상 종료')
                else:
                    self.logger.warning(f'비정상 종료 (코드: {e.code}, 이유: {e.reason})')
                break
            except asyncio.TimeoutError:
                self.logger.warning('웹소켓 메시지 수신 타임아웃')
                continue
            except json.JSONDecodeError as e:
                self.logger.error(f'JSON 파싱 오류: {e}, 메시지: {message[:200] if message else "None"}...', exc_info=True)
                continue
            except Exception as e:
                self.logger.error(f'메시지 수신 오류: {e}', exc_info=True)
                self.logger.error(f'메시지 수신 에러 상세: {traceback.format_exc()}') # type: ignore
                # 연결 종료 대신 계속 시도 (일시적 오류일 수 있음)
                self.logger.warning("메시지 수신 오류 발생, 연결 유지하고 계속 시도")
                
                # 심각한 오류인 경우 잠시 대기
                try:
                    await asyncio.sleep(1)  # 1초 대기
                except Exception as sleep_err:
                    self.logger.error(f"대기 중 오류: {sleep_err}", exc_info=True)
                
                continue
    
    async def subscribe_stock_execution_data(self, codes=None, subscription_type='monitoring'):
        """실시간 주식체결 데이터 구독 (0B)"""
        if codes is None:
            codes = list(self.subscribed_codes)
            
        if codes:
            # 구독된 종목 목록에 추가
            for code in codes:
                self.subscribed_codes.add(code)
            
            # 주식체결 구독 (0B)
            subscribe_data = {
                'trnm': 'REG',  # 서비스명
                'grp_no': '4' if subscription_type == 'monitoring' else '5',  # 그룹번호 (체결 전용)
                'refresh': '1',  # 기존등록유지여부
                'data': [{  # 실시간 등록 리스트
                    'item': codes,  # 실시간 등록 요소
                    'type': ['0B'],  # 실시간 항목 (주식체결)
                }]
            }
            await self.send_message(subscribe_data)
            self.logger.debug(f'📡 실시간 주식체결(0B) 구독 요청: {codes} (그룹: {subscribe_data["grp_no"]})') # type: ignore

    async def unsubscribe_stock_execution_data(self, codes=None):
        """실시간 주식체결 데이터 구독 해제 (0B)"""
        if codes is None:
            codes = list(self.subscribed_codes)
            
        if codes:
            # 구독된 종목 목록에서 제거
            for code in codes:
                self.subscribed_codes.discard(code)
            
            # 주식체결 구독 해제 (0B)
            unsubscribe_data = {
                'trnm': 'UNREG',  # 서비스명
                'grp_no': '4',  # 그룹번호 (체결 전용)
                'data': [{  # 실시간 등록 해제 리스트
                    'item': codes,  # 실시간 등록 해제 요소
                    'type': ['0B'],  # 실시간 항목 (주식체결)
                }]
            }
            await self.send_message(unsubscribe_data)
            self.logger.debug(f'실시간 주식체결 구독 해제 요청: {codes}') # type: ignore

    async def subscribe_order_execution(self):
        """주문체결 실시간 구독 (00) - 키움증권 공식 예시 기반"""
        try:
            grp_no = '1'
            sub_type = '00'
            # 주문체결 실시간 구독 (키움증권 API 문서 참조)
            subscribe_data = {
                'trnm': 'REG',  # 서비스명
                'grp_no': grp_no,  # 그룹번호 (주문체결 전용)
                'refresh': '1',  # 기존등록유지여부
                'data': [{  # 실시간 등록 리스트
                    'item': [''],  # 실시간 등록 요소 (빈 문자열 - 모든 계좌의 주문체결)
                    'type': [sub_type],  # 실시간 항목 (주문체결)
                }]
            }
            # 타입별 그룹 번호 저장
            self._pending_subscriptions[sub_type] = grp_no
            await self.send_message(subscribe_data)
            self.logger.info('✅ 주문체결 실시간 구독 요청 전송 완료')
            
        except Exception as e:
            self.logger.error(f'주문체결 실시간 구독 요청 실패: {e}', exc_info=True)

    async def subscribe_balance(self):
        """실시간 잔고 구독 (04) - 현물잔고"""
        try:
            grp_no = '2'
            sub_type = '04'
            # 실시간 잔고 구독 (키움증권 API 문서 참조)
            subscribe_data = {
                'trnm': 'REG',  # 서비스명
                'grp_no': grp_no,  # 그룹번호 (잔고 전용)
                'refresh': '1',  # 기존등록유지여부
                'data': [{  # 실시간 등록 리스트
                    'item': [''],  # 실시간 등록 요소 (빈 문자열 - 계좌 전체)
                    'type': [sub_type],  # 실시간 항목 (현물잔고)
                }]
            }

            # 타입별 그룹 번호 저장
            self._pending_subscriptions[sub_type] = grp_no
            await self.send_message(subscribe_data)
            self.logger.info('✅ 실시간 잔고 구독 요청 전송 완료')
            
        except Exception as e:
            self.logger.error(f'실시간 잔고 구독 요청 실패: {e}', exc_info=True)

    async def subscribe_market_status(self):
        """시장 상태 구독 (0s) - 키움증권 예시코드 기반"""
        try:
            grp_no = '1'
            sub_type = '0s'
            # 키움증권 예시코드에 따른 시장 상태 구독
            subscribe_data = {
                'trnm': 'REG',  # 서비스명
                'grp_no': grp_no,  # 그룹번호
                'refresh': '1',  # 기존등록유지여부
                'data': [{  # 실시간 등록 리스트
                    'item': [''],  # 실시간 등록 요소 (빈 문자열 - 키움 예시코드 방식)
                    'type': [sub_type],  # 실시간 항목 (시장 상태)
                }]
            }
            # 타입별 그룹 번호 저장
            self._pending_subscriptions[sub_type] = grp_no
            await self.send_message(subscribe_data)
            self.logger.info('✅ 시장 상태 구독 요청 전송 완료')
            
        except Exception as e:
            self.logger.error(f'시장 상태 구독 요청 실패: {e}', exc_info=True)

    async def unsubscribe_market_status(self):
        """시장 상태 구독 해제 (0s)"""
        try:
            grp_no = '1'
            sub_type = '0s'
            # 시장 상태 구독 해제 메시지
            unsubscribe_data = {
                'trnm': 'UNREG',  # 서비스명: 해제
                'grp_no': grp_no,  # 그룹번호
                'data': [{
                    'item': [''],
                    'type': [sub_type],
                }]
            }
            await self.send_message(unsubscribe_data)
            self.logger.info('✅ 시장 상태 구독 해제 요청 전송 완료')

        except Exception as e:
            self.logger.error(f'시장 상태 구독 해제 요청 실패: {e}', exc_info=True)


    def process_balance_data(self, data_item):
        """실시간 잔고 데이터 처리 (웹소켓용)
        주의: 이 메서드는 웹소켓을 통한 실시간 잔고 데이터를 처리합니다.
        REST API 계좌평가현황과는 별개의 데이터입니다.
        """
        try:
            # 실제 키움 API의 실시간 잔고 데이터 구조 파싱
            # data_item 구조: {'type': '04', 'item': 종목코드, 'values': {필드코드: 값}}
            raw_code = data_item.get('item', '') # type: ignore
            stock_code = self.parent.data_manager.normalize_stock_code(raw_code) if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'data_manager') else raw_code  # A 접두사 제거
            values = data_item.get('values', {})
            
            if stock_code and values:
                # 실시간 잔고 수신 내용 로그 추가
                self.logger.debug(f"📬 실시간 잔고 수신 ({stock_code}): {values}")
                
                # 키움 API 실시간 잔고(04) 필드 매핑 (키움증권 웹소켓 API 문서 기준)
                # 참고: type='04' 실시간 잔고 데이터의 values 필드 코드
                # 9201: 계좌번호, 9001: 종목코드, 302: 종목명, 10: 현재가
                # 930: 보유수량, 931: 매입단가, 932: 총매입가(당일누적), 933: 주문가능수량
                # 945: 당일순매수량, 946: 매도/매수구분, 950: 당일총매도손익
                # 990: 당일실현손익(유가), 991: 당일실현손익율(유가)
                stock_name = values.get('302', '')  # 종목명
                current_price_str = values.get('10', '0')  # 현재가
                quantity_str = values.get('930', '0')  # 보유수량
                average_price_str = values.get('931', '0')  # 매입단가
                total_purchase_str = values.get('932', '0')  # 총매입가(당일누적)
                order_available_qty_str = values.get('933', '0')  # 주문가능수량
                daily_net_buy_str = values.get('945', '0')  # 당일순매수량
                daily_total_profit_str = values.get('950', '0')  # 당일총매도손익
                daily_realized_profit_str = values.get('990', '0')  # 당일실현손익(유가)
                daily_realized_profit_rate_str = values.get('991', '0')  # 당일실현손익율(유가)
                
                # 데이터 변환
                quantity = int(quantity_str) if quantity_str else 0
                current_price = float(current_price_str) if current_price_str else 0.0
                average_price = float(average_price_str) if average_price_str else 0.0
                total_purchase = float(total_purchase_str) if total_purchase_str else 0.0
                order_available_qty = int(order_available_qty_str) if order_available_qty_str else 0
                daily_net_buy = int(daily_net_buy_str) if daily_net_buy_str else 0
                daily_total_profit = float(daily_total_profit_str) if daily_total_profit_str else 0.0
                daily_realized_profit = float(daily_realized_profit_str) if daily_realized_profit_str else 0.0
                # 991 필드는 % 값이므로 그대로 사용
                daily_realized_profit_rate = float(daily_realized_profit_rate_str) if daily_realized_profit_rate_str else 0.0 
                
                # 수량이 0보다 큰 경우에만 처리
                if quantity > 0:
                    # 이전 수량을 먼저 저장 (balance_data 업데이트 전에)
                    prev_quantity = 0
                    is_sell_executed = False
                    is_new_stock = stock_code not in self.balance_data # type: ignore
                    
                    # 평가금액 및 평가손익 재계산 (데이터 정확성 확보)
                    # 수수료와 세금을 반영한 실질 손익 계산
                    commission_rate = self.parent.trader.commission_rate
                    tax_rate = self.parent.trader.tax_rate

                    buy_cost_per_share = average_price * (1 + commission_rate)
                    sell_revenue_per_share = current_price * (1 - commission_rate - tax_rate)
                    net_profit_per_share = sell_revenue_per_share - buy_cost_per_share

                    profit_loss = net_profit_per_share * quantity
                    total_buy_cost = buy_cost_per_share * quantity
                    profit_loss_rate = (profit_loss / total_buy_cost) * 100 if total_buy_cost > 0 else 0.0

                    # UI 표시용 평가금액 및 매입금액 (수수료/세금 미포함)
                    evaluation_amount = quantity * current_price
                    purchase_amount = quantity * average_price

                    # 이전 데이터와 비교하여 변경 여부 확인
                    old_info = self.balance_data.get(stock_code, {})
                    price_changed = abs(old_info.get('current_price', 0) - current_price) > 0.01
                    qty_changed = old_info.get('quantity', 0) != quantity
                    pl_changed = abs(old_info.get('profit_loss', 0) - profit_loss) > 0.01

                    is_changed = price_changed or qty_changed or pl_changed

                    if not is_new_stock:
                        prev_quantity = self.balance_data[stock_code].get('quantity', 0)
                    
                    # 잔고 데이터 저장 (기존 종목은 유지, 해당 종목만 업데이트)
                    self.balance_data[stock_code] = {
                        'code': stock_code,
                        'name': stock_name,
                        'quantity': quantity,
                        'average_price': average_price,
                        'current_price': current_price,
                        'evaluation_amount': evaluation_amount, # 재계산된 값으로 업데이트
                        'purchase_amount': purchase_amount, # 재계산된 값으로 업데이트
                        'profit_loss': profit_loss,
                        'profit_loss_rate': profit_loss_rate,
                        'order_available_qty': order_available_qty,
                        'total_purchase': total_purchase,
                        'daily_net_buy': daily_net_buy,
                        'daily_total_profit': daily_total_profit,
                        'daily_realized_profit': daily_realized_profit,
                        'daily_realized_profit_rate': daily_realized_profit_rate,
                        'updated_at': datetime.now().isoformat()
                    }
                    
                    # trader.holdings 자동 동기화 (웹소켓 잔고 업데이트 시, quantity > 0인 경우만)
                    try:
                        if self.parent and hasattr(self.parent, 'trader') and self.parent.trader:
                            trader = self.parent.trader
                            
                            # 보유 종목 추가 또는 업데이트
                            if stock_code not in trader.holdings:
                                # 신규 보유 종목 추가
                                trader.holdings[stock_code] = {'quantity': quantity}
                                # 매입 가격 및 시간 설정 (웹소켓 데이터 활용)
                                if stock_code not in trader.buy_prices:
                                    trader.buy_prices[stock_code] = average_price
                                if stock_code not in trader.buy_times:
                                    trader.buy_times[stock_code] = datetime.now() # type: ignore
                                self.logger.debug(f"🆕 [{stock_code}] trader.holdings에 추가 (수량: {quantity}주, 매입단가: {average_price}원)")
                            else:
                                # 기존 보유 종목 수량 업데이트
                                old_quantity = trader.holdings[stock_code].get('quantity', 0) # type: ignore
                                trader.holdings[stock_code]['quantity'] = quantity
                                # 매입단가가 없으면 웹소켓 평균단가로 업데이트
                                if stock_code not in trader.buy_prices or trader.buy_prices[stock_code] == 0:
                                    trader.buy_prices[stock_code] = average_price
                                # 매입 시간이 없으면 현재 시간으로 설정
                                if stock_code not in trader.buy_times:
                                    trader.buy_times[stock_code] = datetime.now()
                                
                                if old_quantity != quantity:
                                    self.logger.debug(f"🔄 [{stock_code}] trader.holdings 수량 업데이트 ({old_quantity}주 → {quantity}주)")
                    except Exception as sync_ex:
                        self.logger.warning(f"trader.holdings 동기화 실패 ({stock_code}): {sync_ex}", exc_info=True)
                    
                    # 디버그 로그: balance_data 상태
                    if is_new_stock:
                        self.logger.debug(f"🆕 웹소켓 잔고 추가: {stock_code} ({stock_name}) - 현재 보유 종목 수: {len(self.balance_data)}")
                        self.logger.debug(f"   현재 balance_data 키 목록: {list(self.balance_data.keys())}")
                    else:
                        self.logger.debug(f"🔄 웹소켓 잔고 업데이트: {stock_code} (이전 수량: {prev_quantity}, 현재 수량: {quantity})")
                        # balance_data 키 목록 로그 제거 (불필요)
                    
                    # 중요 정보만 표시
                    self.logger.debug(f"📊 실시간 잔고 수신: {stock_name}({stock_code})")
                    self.logger.debug(f"  💰 현재가: {current_price:,.0f}원 | 보유수량: {quantity:,}주 | 매입단가: {average_price:,.0f}원")
                    self.logger.debug(f"  💎 평가금액: {evaluation_amount:,.0f}원 | 매입금액: {purchase_amount:,.0f}원")
                                    
                    # 당일 거래 정보 (있는 경우에만 표시)
                    if daily_net_buy != 0:
                        self.logger.debug(f"  📊 당일순매수량: {daily_net_buy:,}주")
                        
                    # 매도 손익 정보 (매도 체결 시 강조 표시)
                    if daily_total_profit != 0:
                        profit_symbol = "📈" if daily_total_profit > 0 else "📉"
                        self.logger.debug(f"  {profit_symbol} 당일총매도손익: {daily_total_profit:,.0f}원")
                        
                    if daily_realized_profit != 0:
                        profit_symbol = "📈" if daily_realized_profit > 0 else "📉"
                        self.logger.debug(f"  {profit_symbol} 당일실현손익: {daily_realized_profit:,.0f}원 ({daily_realized_profit_rate:+.2f}%)")
                    
                    # 부모 윈도우를 통해 모니터링과 보유종목 리스트에 추가
                    if hasattr(self, 'parent') and self.parent:
                        try:
                            # 메인 스레드에서 실행되도록 QTimer 사용 (람다 클로저 문제 방지)
                            QTimer.singleShot(0, lambda code=stock_code, name=stock_name: self._add_stock_to_ui(code, name))
                        except Exception as ui_err:
                            self.logger.error(f"UI 업데이트 예약 실패: {ui_err}")
                else:
                    # 수량이 0인 경우 → 매도 체결 완료
                    if stock_code in self.balance_data:
                        
                        # 부분 매도 추적 목록에 해당 종목이 있는지 확인
                        is_partial_sell = False
                        if hasattr(self.parent, 'trader') and self.parent.trader:
                            for order_no, details in self.parent.trader.sell_order_details.items():
                                if details.get('code') == stock_code:
                                    is_partial_sell = True
                                    break

                        # 슬랙 알림은 process_order_execution_data에서 직접 계산하여 전송하므로 여기서는 생략합니다.
                        self.logger.debug(f"🔍 매도 체결 전 balance_data: {list(self.balance_data.keys())} ({len(self.balance_data)}개 종목)")     
                        self.logger.info(f"💰 매도 체결 완료: {stock_name}({stock_code})")

                    # 슬랙 알림 전송
                    prev_balance_info = self.balance_data.get(stock_code)
                    if prev_balance_info:
                        sold_qty = prev_balance_info.get('quantity', 0)
                        if hasattr(self.parent, 'login_handler') and self.parent.login_handler.kiwoom_client:
                            asyncio.create_task(self.parent.login_handler.kiwoom_client.send_slack_notification_on_sell(prev_balance_info, daily_realized_profit, daily_realized_profit_rate, sold_qty))
                                                
                        if daily_realized_profit != 0:
                            profit_symbol = "✅" if daily_realized_profit > 0 else "❌"
                            self.logger.info(f"  {profit_symbol} 당일실현손익: {daily_realized_profit:+,.0f}원 ({daily_realized_profit_rate:+.2f}%)")
                                                
                        # 잔고에서 제거
                        del self.balance_data[stock_code]
                        self.logger.debug(f"✅ 잔고에서 제거 완료: {stock_code}")
                        self.logger.debug(f"🔍 제거 후 balance_data: {list(self.balance_data.keys())} ({len(self.balance_data)}개 종목)")
                        
                        # trader.holdings 동기화 강화 (수량이 0일 때 제거)
                        try:
                            if self.parent and hasattr(self.parent, 'trader') and self.parent.trader:
                                trader = self.parent.trader
                                
                                # holdings에서 제거
                                if stock_code in trader.holdings:
                                    del trader.holdings[stock_code]
                                
                                # buy_prices, buy_times도 정리
                                if stock_code in trader.buy_prices:
                                    del trader.buy_prices[stock_code]
                                if stock_code in trader.buy_times:
                                    del trader.buy_times[stock_code]
                                
                                # highest_prices도 정리
                                if stock_code in trader.highest_prices:
                                    del trader.highest_prices[stock_code]
                        except Exception as sync_ex:
                            self.logger.warning(f"trader.holdings 동기화 실패 (전량 매도, {stock_code}): {sync_ex}", exc_info=True)
                        
                        # 최고가 정보도 제거 (objtrader에도 있을 수 있음)
                        if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'objtrader'):
                            if hasattr(self.parent.objtrader, 'highest_prices') and stock_code in self.parent.objtrader.highest_prices:
                                del self.parent.objtrader.highest_prices[stock_code]
                                self.logger.info(f"🗑️ {stock_code} 최고가 정보 제거 완료 (objtrader, 웹소켓 체결)")
            else:
                # 실시간 잔고 데이터 수신 시, UI 테이블 업데이트 트리거
                current_time = time.time()
                if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'update_stock_table'):
                    if current_time - self._last_table_update_time >= self._table_update_interval:
                        QTimer.singleShot(0, self.parent.update_stock_table)
                        self._last_table_update_time = current_time
                self.logger.warning(f"실시간 잔고 데이터 구조 오류: stock_code={stock_code}, values={values}")
                
        except Exception as e:
            self.logger.error(f"실시간 잔고 데이터 처리 실패: {e}", exc_info=True)

    
    def process_order_execution_data(self, data_item):
        """주문체결 실시간 데이터 처리 (type '00')
        
        키움증권 웹소켓 주문체결 실시간 데이터 처리
        - 주문 접수, 체결, 취소, 거부 등의 상태 처리
        - 체결 완료시 보유종목 리스트 자동 업데이트
        """
        try:
            values = data_item.get('values', {})
            
            if not values: # type: ignore
                self.logger.warning("주문체결 데이터가 비어있습니다")
                return
            
            # 키움증권 주문체결(00) 실시간 필드 매핑
            account_no = values.get('9201', '')  # 계좌번호
            order_no = values.get('9203', '')  # 주문번호
            stock_code_raw = values.get('9001', '')  # 종목코드
            stock_code = self.parent.data_manager.normalize_stock_code(stock_code_raw) if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'data_manager') else stock_code_raw
            stock_name = values.get('302', '')  # 종목명
            order_status = values.get('913', '')  # 주문상태: 접수, 체결, 확인, 취소, 거부
            order_type = values.get('905', '')  # 주문구분: 매도, 매수, 정정, 취소 등
            trade_type = values.get('906', '')  # 매매구분: 보통, 시장가 등
            buy_sell_flag = values.get('907', '')  # 매도수구분: 1=매도, 2=매수
            order_qty = values.get('900', '0')  # 주문수량
            order_price = values.get('901', '0')  # 주문가격
            unfilled_qty = values.get('902', '0')  # 미체결수량
            exec_price = values.get('910', '0')  # 체결가
            exec_qty = values.get('911', '0')  # 체결량
            exec_no = values.get('909', '')  # 체결번호
            exec_time = values.get('908', '')  # 주문/체결시간
            reject_reason = values.get('919', '')  # 거부사유
            
            # 데이터 변환
            order_qty_int = int(order_qty) if order_qty else 0
            unfilled_qty_int = int(unfilled_qty) if unfilled_qty else 0
            exec_qty_int = int(exec_qty) if exec_qty else 0
            exec_price_float = float(exec_price) if exec_price else 0.0
            
            # 로그 출력 (상태별)
            
            # 주문상태별 아이콘
            status_icon = {
                '접수': '📥',
                '체결': '✅',
                '확인': 'ℹ️',
                '취소': '❌',
                '거부': '🚫'
            }.get(order_status, '❓')
            
            # 매수/매도 구분 아이콘
            trade_icon = '🔴' if buy_sell_flag == '1' else '🔵'  # 1=매도(빨강), 2=매수(파랑)
            
            self.logger.debug(f"{status_icon} 주문체결 실시간 수신: {order_status}")
            self.logger.debug(f"  {trade_icon} 종목: {stock_name}({stock_code})")
            self.logger.debug(f"  📋 주문구분: {order_type} | 매매구분: {trade_type}")
            self.logger.debug(f"  🔢 주문번호: {order_no} | 계좌: {account_no}")
            
            if order_status == '체결':
                self.logger.debug(f"  💰 체결가: {exec_price_float:,.0f}원 | 체결량: {exec_qty_int:,}주")
                self.logger.debug(f"  📊 미체결수량: {unfilled_qty_int:,}주 / 주문수량: {order_qty_int:,}주")
                self.logger.debug(f"  ⏰ 체결시간: {exec_time} | 체결번호: {exec_no}")
            elif order_status == '접수':
                self.logger.debug(f"  💵 주문가: {order_price}원 | 주문수량: {order_qty_int:,}주")
            elif order_status == '거부':
                self.logger.debug(f"  ⚠️ 거부사유: {reject_reason}")
            
            
            # 부분 매도 주문 완료 시 슬랙 알림
            if order_status == '체결' and order_no in self.parent.trader.sell_order_details:
                order_detail = self.parent.trader.sell_order_details[order_no]
                order_detail['filled_qty'] += exec_qty_int
                
                self.logger.debug(f"📊 부분 매도 체결 진행: 주문번호={order_no}, 체결량={exec_qty_int}주, 누적체결량={order_detail['filled_qty']}/{order_detail['total_qty']}")

                # 주문 수량이 모두 체결되었는지 확인
                if order_detail['filled_qty'] >= order_detail['total_qty']:
                    self.logger.info(f"🎉 부분 매도 주문 완료: {stock_name}({stock_code}) {order_detail['total_qty']}주")

                    # 추적 목록에서 제거
                    del self.parent.trader.sell_order_details[order_no]

            # 체결 완료 확인: 주문상태='체결' AND 미체결수량=0
            if order_status == '체결' and unfilled_qty_int == 0:                
                # 매수 체결 완료 → 보유종목 리스트에 추가 (람다 클로저 문제 방지)
                # 매도 체결 완료 시 실현 손익 직접 계산 및 알림
                # 손익 계산 및 로깅은 process_balance_data에서 처리하므로 여기서는 제거합니다.

                if buy_sell_flag == '2': # '2'는 매수를 의미

                    # '매수 주문 진행 중' 상태 해제
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader'):
                        if stock_code in self.parent.trader.pending_buy_orders: # type: ignore
                            self.parent.trader.pending_buy_orders.discard(stock_code)

                    if stock_code in self.balance_data:
                        # 매수 체결 완료 로그 표시
                        self.logger.info(f"💰 매수 체결 완료: {stock_name}({stock_code})")

                    if hasattr(self, 'parent') and self.parent:
                        QTimer.singleShot(0, lambda code=stock_code, name=stock_name: self._add_stock_to_ui(code, name))
                
                # 매도 체결 완료 → 보유종목 리스트에서 제거 (람다 클로저 문제 방지)
                elif buy_sell_flag == '1': # '1'은 매도를 의미
                    # 매도 체결 완료 시 실현 손익 직접 계산 및 알림
                    # process_balance_data에서도 처리되지만, 주문체결 시점에서 더 빠르게 처리하기 위해 여기서도 수행
                    prev_balance_info = self.balance_data.get(stock_code)
                    if prev_balance_info:
                        sold_qty = prev_balance_info.get('quantity', 0)
                        daily_realized_profit = float(values.get('990', '0'))
                        daily_realized_profit_rate = float(values.get('991', '0'))
                        asyncio.create_task(self.parent.login_handler.kiwoom_client.send_slack_notification_on_sell(prev_balance_info, daily_realized_profit, daily_realized_profit_rate, sold_qty))

                    self.logger.debug(f"✅ 매도 체결 완료 → 보유종목에서 제거: {stock_code}")

                    # '주문 진행 중' 상태 해제
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader'):
                        if stock_code in self.parent.trader.pending_sell_orders: # type: ignore
                            self.parent.trader.pending_sell_orders.discard(stock_code)
                    
                    # balance_data에서 즉시 제거하여 UI 불일치 방지
                    if stock_code in self.balance_data:
                        # 매도 체결 완료 로그 표시                   
                        self.logger.info(f"💰 매도 체결 완료: {stock_name}({stock_code})")

                    # UI 업데이트 (보유종목 리스트 및 투자현황표)
                    if hasattr(self, 'parent') and self.parent:
                        QTimer.singleShot(0, lambda code=stock_code: self._remove_stock_from_ui(code))

                    # 최고가 정보도 제거 (UI 업데이트 후)
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'objtrader'):
                        if hasattr(self.parent.objtrader, 'highest_prices') and stock_code in self.parent.objtrader.highest_prices:
                            del self.parent.objtrader.highest_prices[stock_code]
                            self.logger.info(f"🗑️ {stock_code} 최고가 정보 제거 완료 (주문 체결)")
            
        except Exception as e:
            self.logger.error(f"주문체결 데이터 처리 실패: {e}", exc_info=True)
            
    
    def _add_stock_to_ui(self, stock_code, stock_name):
        """UI에 종목 추가 (메인 스레드에서 실행)"""
        try:
            if not hasattr(self, 'parent') or not self.parent:
                return
            
            # 1. 모니터링 리스트에 추가
            monitoring_exists = False # type: ignore
            for i in range(self.parent.trading_tab.monitoringBox.count()):
                item_text = self.parent.trading_tab.monitoringBox.item(i).text()
                # 종목코드 추출 (종목명 유무와 관계없이)                
                if item_text == stock_code:
                    monitoring_exists = True
                    break
            
            if not monitoring_exists:
                self.parent.monitoring_manager.add_stock_to_monitoring(stock_code, None)
                self.logger.info(f"✅ 모니터링 리스트에 추가: {stock_code} ({stock_name})")
            
            # 2. 보유종목 리스트에 추가
            holding_exists = False
            for i in range(self.parent.trading_tab.boughtBox.count()):
                item_text = self.parent.trading_tab.boughtBox.item(i).text()
                # 종목코드 추출 (종목명 유무와 관계없이)                
                if item_text == stock_code:
                    holding_exists = True
                    break
            
            if not holding_exists:
                self.parent.trading_tab.boughtBox.addItem(stock_code)
                self.logger.debug(f"✅ 보유종목 리스트에 추가: {stock_code} ({stock_name})")
            
            # 3. 투자 현황표 업데이트
            if hasattr(self.parent, 'update_stock_table'):
                # 디버그 로그: 투자 현황표 업데이트 전 balance_data 상태
                if hasattr(self, 'balance_data'):
                    self.logger.debug(f"🔍 투자 현황표 업데이트 전 WebSocket balance_data: {list(self.balance_data.keys())} ({len(self.balance_data)}개 종목)") # type: ignore
                self.parent.update_stock_table()
                
        except Exception as e:
            self.logger.error(f"UI 종목 추가 실패 ({stock_code}): {e}", exc_info=True)
            
    
    def _add_stock_to_ui(self, stock_code, stock_name):
        """UI에 종목 추가 (메인 스레드에서 실행)"""
        try:
            if not hasattr(self, 'parent') or not self.parent:
                return
            
            # 보유종목 리스트에 추가
            holding_exists = False
            for i in range(self.parent.trading_tab.boughtBox.count()):
                item_text = self.parent.trading_tab.boughtBox.item(i).text()
                if stock_code in item_text:
                    holding_exists = True
                    break
            
            if not holding_exists:
                self.parent.trading_tab.boughtBox.addItem(stock_code)
                self.logger.info(f"✅ 보유종목 리스트에 추가: {stock_code} ({stock_name})")
            
            # 투자 현황표 업데이트
            if hasattr(self.parent, 'update_stock_table'):
                self.parent.update_stock_table()
                
        except Exception as e:
            self.logger.error(f"UI 종목 추가 실패 ({stock_code}): {e}", exc_info=True)
            
    
    def _remove_stock_from_ui(self, stock_code):
        """UI에서 종목 제거 (메인 스레드에서 실행)"""
        try:
            if not hasattr(self, 'parent') or not self.parent:
                return
            
            # 보유종목 리스트에서 제거
            for i in range(self.parent.trading_tab.boughtBox.count()):
                item_text = self.parent.trading_tab.boughtBox.item(i).text()
                # 종목코드 추출 (종목명 유무와 관계없이)                
                if item_text.split()[0] == stock_code:
                    self.parent.trading_tab.boughtBox.takeItem(i)
                    logging.debug(f"✅ 보유종목 리스트에서 제거: {stock_code}")

                    # 모니터링 리스트에도 없는 경우에만 차트 초기화
                    is_still_monitoring = False
                    if hasattr(self.parent.trading_tab, 'monitoringBox'):
                        for j in range(self.parent.trading_tab.monitoringBox.count()):
                            monitoring_item = self.parent.trading_tab.monitoringBox.item(j)
                            if monitoring_item and monitoring_item.text().split()[0] == stock_code:
                                is_still_monitoring = True
                                break
                    
                    if not is_still_monitoring:
                        # 차트 위젯이 제거된 종목을 표시하고 있었다면 차트 초기화
                        if hasattr(self.parent, 'trading_tab') and hasattr(self.parent.trading_tab, 'realtime_chart_widget'):
                            chart_widget = self.parent.trading_tab.realtime_chart_widget
                            if chart_widget.current_code == stock_code:
                                self.logger.debug(f"현재 차트 종목({stock_code})이 보유 및 모니터링 목록에서 모두 제거되어 차트를 초기화합니다.")
                                chart_widget.set_current_code(None)
                    break
            
            # 투자 현황표 업데이트
            if hasattr(self.parent, 'update_stock_table'):
                self.parent.update_stock_table()
                    
        except Exception as e:
            self.logger.error(f"UI 종목 제거 실패 ({stock_code}): {e}", exc_info=True)

    def process_stock_execution_data(self, data_item):
        """실시간 주식 데이터 처리 (type='0B' 주식체결)"""
        try:            
            # data_item에서 실시간 데이터 추출
            if 'item' in data_item and 'values' in data_item:
                raw_code = data_item['item']
                stock_code = self.parent.data_manager.normalize_stock_code(raw_code) if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'data_manager') else raw_code  # A 접두사 제거
                values = data_item['values']
                data_type = data_item.get('type', '0B')  # 데이터 타입 확인 (기본값: 0B)
                
                if stock_code and values:
                    # 현재가 추출 (type='0B' 필드 '10' 사용)
                    current_price_raw = values.get('10', '0')
                    
                    try:
                        current_price = float(current_price_raw.replace('+', '').replace('-', '').replace(',', ''))
                    except (ValueError, AttributeError):
                        self.logger.warning(f"현재가 파싱 실패: {current_price_raw}")
                        return
                    
                    # type='0B' (주식 체결): 차트 업데이트 및 현재가 업데이트
                    if data_type == '0B':
                        # 추가 필드 추출 (체결 데이터 전용)
                        execution_time = values.get('20', '')
                        volume_raw = values.get('15', '0')
                        strength_raw = values.get('228', '0')
                        
                        try:
                            volume = int(volume_raw.replace('+', '').replace('-', '').replace(',', ''))
                        except (ValueError, AttributeError):
                            volume = 0
                        
                        try:
                            strength = float(strength_raw.replace('%', '').replace(',', ''))
                        except (ValueError, AttributeError):
                            strength = 0.0                       
                        
                        # 체결 데이터를 딕셔너리로 생성
                        execution_info = {
                            'execution_time': execution_time, # type: ignore
                            'current_price': current_price,
                            'volume': volume,
                            'strength': strength,
                        }
                        
                        # 보유 종목이면 balance_data의 현재가 업데이트
                        if stock_code in self.balance_data:
                            self._update_holding_current_price(stock_code, current_price)
                        
                        # 실시간 데이터를 차트 데이터에 추가
                        self._add_realtime_data_to_chart(stock_code, execution_info)
                        
                        return
                    
                    else:
                        self.logger.warning(f"알 수 없는 데이터 타입: {data_type}")
                        return

                else:
                    self.logger.warning("실시간 데이터에서 종목코드를 찾을 수 없습니다")
            else:
                self.logger.warning("실시간 데이터에 item 정보가 없습니다")
                
        except Exception as e:
            self.logger.error(f"실시간 데이터 처리 실패: {e}", exc_info=True)
            
    
    async def process_stock_execution_data_async(self, data_item):
        """실시간 주식 데이터 처리 - 비동기 버전 (기술적 지표 계산 비동기화)"""
        try:
            # 원래 동기 함수의 로직을 거의 동일하게 유지
            # data_item에서 실시간 데이터 추출
            if 'item' in data_item and 'values' in data_item:
                raw_code = data_item['item']
                stock_code = self.parent.data_manager.normalize_stock_code(raw_code) if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'data_manager') else raw_code  # A 접두사 제거
                values = data_item['values']
                data_type = data_item.get('type', '0B')  # 데이터 타입 확인 (기본값: 0B)
                
                if stock_code and values:
                    # 현재가 추출 (type='0B' 필드 '10' 사용)
                    current_price_raw = values.get('10', '0')
                    
                    try:
                        current_price = float(current_price_raw.replace('+', '').replace('-', '').replace(',', ''))
                    except (ValueError, AttributeError):
                        self.logger.warning(f"현재가 파싱 실패: {current_price_raw}")
                        return
                    
                    # type='0B' (주식 체결): 차트 업데이트 및 현재가 업데이트
                    if data_type == '0B':
                        # 추가 필드 추출 (체결 데이터 전용)
                        execution_time = values.get('20', '')
                        volume_raw = values.get('15', '0')
                        strength_raw = values.get('228', '0')
                        
                        try:
                            volume = int(volume_raw.replace('+', '').replace('-', '').replace(',', ''))
                        except (ValueError, AttributeError):
                            volume = 0
                        
                        try:
                            strength = float(strength_raw.replace('%', '').replace(',', ''))
                        except (ValueError, AttributeError):
                            strength = 0.0                       
                        
                        # 체결 데이터를 딕셔너리로 생성
                        execution_info = {
                            'execution_time': execution_time, # type: ignore
                            'current_price': current_price,
                            'volume': volume,
                            'strength': strength,
                        }
                        
                        # 보유 종목이면 balance_data의 현재가 업데이트
                        if stock_code in self.balance_data:
                            self._update_holding_current_price(stock_code, current_price)
                        
                        # ChartDataCache에 실시간 데이터 처리를 위임
                        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                            await self.parent.chart_cache.add_realtime_data_async(stock_code, execution_info)
                    
                    else:
                        self.logger.warning(f"알 수 없는 데이터 타입: {data_type}")
                        return

                else:
                    self.logger.warning("실시간 데이터에서 종목코드를 찾을 수 없습니다")
            else:
                self.logger.warning("실시간 데이터에 item 정보가 없습니다")
                
        except Exception as e:
            self.logger.error(f"실시간 데이터 처리 실패: {e}", exc_info=True)
            
    
    def _update_holding_current_price(self, stock_code, current_price):
        """보유 종목의 실시간 현재가 업데이트 및 손익 재계산"""
        try:
            if stock_code not in self.balance_data:
                return
            
            stock_info = self.balance_data[stock_code]
            
            # 현재가가 실제로 변경되었을 때만 업데이트
            old_price = stock_info.get('current_price', 0)
            if abs(current_price - old_price) < 0.01:  # 가격 변동이 거의 없으면 스킵
                return
            
            # 현재가 업데이트
            stock_info['current_price'] = current_price
            
            # 평가금액 및 손익 재계산
            quantity = stock_info.get('quantity', 0)
            average_price = stock_info.get('average_price', 0)
            
            # 수수료와 세금을 반영한 실질 손익 계산
            commission_rate = self.parent.trader.commission_rate
            tax_rate = self.parent.trader.tax_rate

            buy_cost_per_share = average_price * (1 + commission_rate)
            sell_revenue_per_share = current_price * (1 - commission_rate - tax_rate)
            net_profit_per_share = sell_revenue_per_share - buy_cost_per_share

            profit_loss = net_profit_per_share * quantity
            total_buy_cost = buy_cost_per_share * quantity
            profit_loss_rate = (profit_loss / total_buy_cost) * 100 if total_buy_cost > 0 else 0.0
            
            evaluation_amount = quantity * current_price # UI 표시용
            # 업데이트된 값 저장
            stock_info['evaluation_amount'] = evaluation_amount
            stock_info['profit_loss'] = profit_loss
            stock_info['profit_loss_rate'] = profit_loss_rate
            stock_info['updated_at'] = datetime.now().isoformat()
            
            # balance_data 업데이트
            self.balance_data[stock_code] = stock_info
            
            # parent.trader.holdings도 동기화 (매도 평가를 위한 현재가 업데이트)
            if hasattr(self, 'parent') and self.parent:
                if hasattr(self.parent, 'trader') and self.parent.trader:
                    trader = self.parent.trader
                    if hasattr(trader, 'holdings') and stock_code in trader.holdings:
                        trader.holdings[stock_code]['current_price'] = current_price
                        # holdings 업데이트 로그는 너무 빈번하므로 DEBUG 레벨로 유지
                        # self.logger.debug(f"✅ holdings 현재가 업데이트: {stock_code} {current_price:,}원")
            
            # 투자현황표 업데이트 (throttling 적용)
            current_time = time.time()
            if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trading_tab'):
                # 마지막 업데이트로부터 일정 시간(1초)이 지난 경우에만 업데이트
                if current_time - self._last_table_update_time >= self._table_update_interval:
                    # QTimer를 사용하여 메인 스레드에서 UI 업데이트를 예약합니다.
                    QTimer.singleShot(0, self.parent.update_stock_table)
                    self._last_table_update_time = current_time

                    # 보유종목 리스트(boughtBox)도 balance_data 기준으로 동기화
                    try:
                        if hasattr(self.parent, 'trading_tab') and hasattr(self.parent.trading_tab, 'boughtBox'):
                            bought_box = self.parent.trading_tab.boughtBox
                            
                            # 현재 리스트박스의 종목 코드들을 set으로 변환
                            current_list_codes = set()
                            for i in range(bought_box.count()):
                                item = bought_box.item(i)
                                if item:
                                    current_list_codes.add(item.text().split()[0])

                            # balance_data의 종목 코드들을 set으로 변환
                            balance_codes = set(self.balance_data.keys())

                            # 두 set을 비교하여 UI 업데이트
                            # 추가해야 할 종목
                            for code_to_add in balance_codes - current_list_codes:
                                bought_box.addItem(code_to_add)
                                self.logger.debug(f"✅ 보유종목 리스트 동기화 (추가): {code_to_add}")

                            # 제거해야 할 종목
                            for code_to_remove in current_list_codes - balance_codes:
                                for i in range(bought_box.count()):
                                    item = bought_box.item(i)
                                    if item and item.text().split()[0] == code_to_remove:
                                        bought_box.takeItem(i)
                                        self.logger.debug(f"✅ 보유종목 리스트 동기화 (제거): {code_to_remove}")
                                        break # 해당 아이템을 찾았으면 루프 종료

                    except Exception as box_sync_ex:
                        self.logger.error(f"보유종목 리스트 동기화 실패: {box_sync_ex}", exc_info=True)

                else:
                    # throttling에 걸린 경우, 전체 테이블 업데이트 대신 로그만 남김
                    self.logger.debug(f"📊 실시간 시세 반영 (UI 업데이트 보류 - throttling): {stock_code} {old_price:,.0f}원 → {current_price:,.0f}원")

        except Exception as e:
            self.logger.error(f"보유 종목 현재가 업데이트 실패 ({stock_code}): {e}", exc_info=True)
    
    def _update_tic_chart_with_realtime(self, stock_code, cached_data, realtime_data):
        """틱 차트에 실시간 데이터 추가 (30틱 = 1봉) - 통합된 함수"""
        try:
            # cached_data가 None이거나 dict가 아니면 리턴
            if not cached_data or not isinstance(cached_data, dict):
                logging.debug(f"⚠️ 틱 차트 업데이트 건너뜀: {stock_code} (캐시 데이터 없음)")
                return
            
            tic_data = cached_data.get('tic_data', {})
            if not tic_data:
                logging.debug(f"⚠️ 틱 차트 업데이트 건너뜀: {stock_code} (틱 데이터 없음)")
                return
            
            # 필수 키가 없으면 초기화
            required_keys = ['time', 'open', 'high', 'low', 'close', 'volume', 'strength']
            for key in required_keys:
                if key not in tic_data:
                    tic_data[key] = []
            
            # 실시간 데이터에서 시간 파싱
            execution_time = realtime_data.get('execution_time', '')
            if not execution_time:
                return
            
            # 시간을 datetime 객체로 변환
            try:
                if len(execution_time) == 6:  # HHMMSS
                    today = datetime.now().strftime('%Y%m%d')
                    full_time = f"{today}{execution_time}"
                    dt = datetime.strptime(full_time, '%Y%m%d%H%M%S')
                elif len(execution_time) == 14:  # YYYYMMDDHHMMSS
                    dt = datetime.strptime(execution_time, '%Y%m%d%H%M%S')
                else:
                    dt = datetime.now()
            except:
                dt = datetime.now()
            
            # 틱 데이터에 실시간 데이터 추가 (음수 값 보정)
            current_price = abs(realtime_data.get('current_price', 0))  # 음수면 양수로 전환
            volume = abs(realtime_data.get('volume', 0))  # 음수면 양수로 전환
            strength = abs(realtime_data.get('strength', 0))  # 음수면 양수로 전환
            
            # API 조회의 마지막 틱 개수 확인
            last_tic_cnt = tic_data.get('last_tic_cnt', 0)
            
            # last_tic_cnt 타입 검증 및 변환
            if isinstance(last_tic_cnt, list) and len(last_tic_cnt) > 0:
                last_tic_cnt = last_tic_cnt[0]
            
            # 정수로 변환 시도
            try:
                last_tic_cnt = int(last_tic_cnt)
            except (ValueError, TypeError):
                last_tic_cnt = 0
            
            # 기존 봉이 없는 경우 (초기 상태)
            if len(tic_data.get('close', [])) == 0:
                # 첫 봉 생성
                tic_data['time'].append(dt) # type: ignore
                tic_data['open'].append(current_price)
                tic_data['high'].append(current_price)
                tic_data['low'].append(current_price)
                tic_data['close'].append(current_price)
                tic_data['volume'].append(volume)
                tic_data['strength'].append(strength)
                tic_data['last_tic_cnt'] = 1
                self.logger.info(f"🎯 첫 번째 60틱봉 생성: {stock_code}, 가격={current_price}")
            elif last_tic_cnt < 60:
                # 60틱 미만이면 기존 봉 업데이트
                last_index = -1
                
                # 종가 업데이트
                tic_data['close'][last_index] = current_price
                
                # 고가 업데이트 (현재가가 더 높으면)
                if tic_data['high'][last_index] < current_price:
                    tic_data['high'][last_index] = current_price
                
                # 저가 업데이트 (현재가가 더 낮으면)
                if tic_data['low'][last_index] > current_price:
                    tic_data['low'][last_index] = current_price
                
                # 거래량 누적
                tic_data['volume'][last_index] += volume
                
                # 체결강도를 실시간 체결강도로 업데이트
                tic_data['strength'][last_index] = strength

                # 마지막 틱 개수 증가
                tic_data['last_tic_cnt'] = last_tic_cnt + 1                
            else: # last_tic_cnt >= 60
                # 60틱이 되면 새로운 봉 생성
                tic_data['time'].append(dt)
                tic_data['open'].append(current_price)
                tic_data['high'].append(current_price)
                tic_data['low'].append(current_price)
                tic_data['close'].append(current_price)
                tic_data['volume'].append(volume)
                tic_data['strength'].append(strength)
                # 틱 카운트를 1로 리셋 (새 봉의 첫 번째 틱)
                tic_data['last_tic_cnt'] = 1
            
            # 최대 데이터 수 제한 (300개)
            max_data = 300
            for key in ['time', 'open', 'high', 'low', 'close', 'volume', 'strength']:
                if key in tic_data and len(tic_data[key]) > max_data:
                    tic_data[key] = tic_data[key][-max_data:]
                        
        except Exception as e:
            self.logger.error(f"틱 차트 실시간 데이터 추가 실패: {e}", exc_info=True)
    
    def _update_minute_chart_with_realtime(self, stock_code, cached_data, realtime_data):
        """분봉 차트에 실시간 데이터 추가 (3분 = 1봉)"""
        try:
            # cached_data가 None이거나 dict가 아니면 리턴
            if not cached_data or not isinstance(cached_data, dict):
                self.logger.debug(f"⚠️ 분봉 차트 업데이트 건너뜀: {stock_code} (캐시 데이터 없음)")
                return
            
            min_data = cached_data.get('min_data', {})
            if not min_data:
                self.logger.debug(f"⚠️ 분봉 차트 업데이트 건너뜀: {stock_code} (분봉 데이터 없음)")
                return
            
            # 필수 키가 없으면 초기화
            required_keys = ['time', 'open', 'high', 'low', 'close', 'volume']
            for key in required_keys:
                if key not in min_data:
                    min_data[key] = []
            
            # 실시간 데이터에서 시간 파싱
            execution_time = realtime_data.get('execution_time', '')
            if not execution_time:
                return
            
            # 시간을 datetime 객체로 변환
            try:
                if len(execution_time) == 6:  # HHMMSS
                    today = datetime.now().strftime('%Y%m%d')
                    full_time = f"{today}{execution_time}"
                    dt = datetime.strptime(full_time, '%Y%m%d%H%M%S')
                elif len(execution_time) == 14:  # YYYYMMDDHHMMSS
                    dt = datetime.strptime(execution_time, '%Y%m%d%H%M%S')
                else:
                    dt = datetime.now()
            except:
                dt = datetime.now()
            
            # 3분 단위로 시간 정규화
            minute = dt.minute
            normalized_minute = (minute // 3) * 3
            normalized_dt = dt.replace(minute=normalized_minute, second=0, microsecond=0)
            
            current_price = abs(realtime_data.get('current_price', 0))  # 음수면 양수로 전환
            volume = abs(realtime_data.get('volume', 0))  # 음수면 양수로 전환
            
            # 기존 봉이 없는 경우 (초기 상태)
            if len(min_data.get('close', [])) == 0:
                # 첫 봉 생성 # type: ignore
                min_data['time'].append(normalized_dt)
                min_data['open'].append(current_price)
                min_data['high'].append(current_price)
                min_data['low'].append(current_price)
                min_data['close'].append(current_price)
                min_data['volume'].append(volume)
                
                self.logger.info(f"🎯 첫 번째 3분봉 생성: {stock_code}, 시간={normalized_dt.strftime('%H:%M:%S')}, 가격={current_price}")
                return
            
            # 기존 분봉 데이터 확인
            last_time = min_data['time'][-1] # type: ignore
            
            # 같은 3분 구간인지 확인
            if last_time == normalized_dt:
                # 기존 봉 업데이트
                min_data['close'][-1] = current_price
                if min_data['high'][-1] < current_price:
                    min_data['high'][-1] = current_price
                if min_data['low'][-1] > current_price:
                    min_data['low'][-1] = current_price
                min_data['volume'][-1] += volume
                
                # 기존 봉 업데이트 로그 표시
                self._log_last_minute_bar_data(stock_code, min_data, -1)
            else:
                # 새로운 봉 생성 # type: ignore
                min_data['time'].append(normalized_dt)
                min_data['open'].append(current_price)
                min_data['high'].append(current_price)
                min_data['low'].append(current_price)
                min_data['close'].append(current_price)
                min_data['volume'].append(volume)
                
                # 새로운 3분봉 생성 로그
                self.logger.debug(f"🕐 새로운 3분봉 생성: {stock_code}, 시간: {normalized_dt.strftime('%H:%M:%S')}")
                
                # 새로운 3분봉 생성 시 마지막 봉 데이터 로그 표시
                self._log_last_minute_bar_data(stock_code, min_data, -1)                    
            
            # 최대 데이터 수 제한 (150개)
            max_data = 150
            for key in ['time', 'open', 'high', 'low', 'close', 'volume']:
                if key in min_data and len(min_data[key]) > max_data:
                    min_data[key] = min_data[key][-max_data:]
            
        except Exception as e:
            self.logger.error(f"분봉 차트 실시간 데이터 추가 실패: {e}", exc_info=True)
    
    def _log_last_minute_bar_data(self, stock_code, min_data, bar_index):
        """마지막 분봉 데이터를 로그에 표시"""
        try:
            if not min_data or not min_data.get('time') or len(min_data['time']) == 0:
                return
            
            # 종목명 조회
            stock_name = self.get_stock_name(stock_code) if hasattr(self, 'get_stock_name') else stock_code
            
            # 마지막 봉 데이터 추출
            time_str = min_data['time'][bar_index].strftime('%H:%M:%S') if hasattr(min_data['time'][bar_index], 'strftime') else str(min_data['time'][bar_index])
            open_price = min_data['open'][bar_index] if bar_index < len(min_data['open']) else 0
            high_price = min_data['high'][bar_index] if bar_index < len(min_data['high']) else 0
            low_price = min_data['low'][bar_index] if bar_index < len(min_data['low']) else 0
            close_price = min_data['close'][bar_index] if bar_index < len(min_data['close']) else 0
            volume = min_data['volume'][bar_index] if bar_index < len(min_data['volume']) else 0
            
        except Exception as e:
            self.logger.error(f"분봉 데이터 로그 표시 실패: {e}", exc_info=True)
    
    def _log_last_tic_bar_data(self, stock_code, tic_data, bar_index):
        """마지막 틱 봉 데이터를 로그에 표시"""
        try:
            if 'tic_bars' not in tic_data or not tic_data:
                return
            
            bars = tic_data
            if not bars.get('time') or len(bars['time']) == 0:
                return
            
            # 종목명 조회
            stock_name = self.get_stock_name(stock_code) if hasattr(self, 'get_stock_name') else stock_code
            
            # 마지막 봉 데이터 추출
            time_str = bars['time'][bar_index].strftime('%H:%M:%S') if hasattr(bars['time'][bar_index], 'strftime') else str(bars['time'][bar_index])
            open_price = bars['open'][bar_index] if bar_index < len(bars['open']) else 0
            high_price = bars['high'][bar_index] if bar_index < len(bars['high']) else 0
            low_price = bars['low'][bar_index] if bar_index < len(bars['low']) else 0
            close_price = bars['close'][bar_index] if bar_index < len(bars['close']) else 0
            volume = bars['volume'][bar_index] if bar_index < len(bars['volume']) else 0
            strength = bars['strength'][bar_index] if bar_index < len(bars['strength']) else 0
            
            # 로그 출력
            self.logger.info(f"📊 {stock_name}({stock_code}) - 30틱 봉 업데이트")
            if strength > 0:
                self.logger.info(f"   💪 체결강도: {strength:.1f}%")
            
        except Exception as e:
            self.logger.error(f"틱 봉 데이터 로그 표시 실패: {e}", exc_info=True)

    def process_condition_realtime_notification(self, data_item):
        """조건검색 실시간 알림 처리"""
        try:
            # 조건검색 실시간 알림 데이터 처리
            self.logger.debug(f"조건검색 실시간 알림 데이터: {data_item}")
            
            # 데이터 구조 확인 및 파싱
            item_data = data_item.get('item', {})
            values = data_item.get('values', {}) # type: ignore
            
            # values에서 추가 정보 추출
            action_type = None  # 편입/이탈 구분
            condition_seq = None  # 조건검색식 순번
            condition_name = "조건검색" # 기본값
            stock_code = None
            
            if values and isinstance(values, dict):
                stock_code = values.get('9001')  # 종목코드
                action_type = values.get('843', 'I')         # 편입/이탈 구분 ('I'=편입, 'D'=이탈)
                condition_seq = values.get('841', '0')       # 조건검색식 순번
            else:
                action_type = 'I'  # 기본값은 편입(INSERT)
            
            # stock_code가 values에 없는 경우 item_data에서 찾기
            if not stock_code:
                stock_code = item_data if isinstance(item_data, str) else (item_data.get('code', '') if isinstance(item_data, dict) else '')

            # 조건검색식 순번(seq)으로 실제 조건식 이름 찾기
            if condition_seq and hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                for cond in self.parent.condition_search_list:
                    if cond.get('seq') == condition_seq:
                        condition_name = cond.get('title', '조건검색')
                        break
            
            if stock_code:
                # 액션 타입에 따른 처리
                if action_type == 'I':  # INSERT (편입) # type: ignore
                    self.logger.info(f"📈 조건검색 실시간 편입: {stock_code} ({condition_name}, seq: {condition_seq})")
                    # 부모 윈도우에 종목 추가 요청 (비동기)
                    if hasattr(self, 'parent') and self.parent:
                        # chart_cache를 통해 API 큐에 추가 # type: ignore
                        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                            result = self.parent.chart_cache.add_stock_to_api_queue(stock_code)
                            if result:
                                self.logger.debug(f"✅ 조건검색 편입 종목 API 큐 추가 성공: {stock_code}")
                            else:
                                self.logger.debug(f"ℹ️ 조건검색 편입 종목 이미 존재 또는 중복: {stock_code}")
                        else:
                            self.logger.error(f"❌ chart_cache가 없습니다: {stock_code}")
                elif action_type == 'D':  # DELETE (이탈) # type: ignore
                    self.logger.info(f"📉 조건검색 실시간 이탈: {stock_code} ({condition_name}, seq: {condition_seq})")
                    
                    # 보유 종목인지 확인
                    is_holding = False
                    if hasattr(self, 'parent') and self.parent:
                        # boughtBox (보유종목 리스트)에서 확인
                        if hasattr(self.parent.trading_tab, 'boughtBox'):
                            for i in range(self.parent.trading_tab.boughtBox.count()):
                                item = self.parent.trading_tab.boughtBox.item(i)
                                if item and stock_code in item.text():
                                    is_holding = True
                                    break
                    
                    # 보유 중인 종목은 모니터링에서 제거하지 않음
                    if is_holding:
                        self.logger.debug(f"✅ 보유 종목이므로 모니터링 유지: {stock_code}")
                    else:
                        # 보유하지 않은 종목만 모니터링에서 제거
                        if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'monitoring_manager'):
                            asyncio.create_task(self.parent.monitoring_manager.remove_stock_from_monitoring(stock_code))

                else:
                    self.logger.warning(f"⚠️ 알 수 없는 조건검색 액션 타입: {stock_code} - 액션: {action_type}") # type: ignore
            else:
                self.logger.warning("⚠️ 조건검색 실시간 알림에서 종목코드를 찾을 수 없습니다")

        except Exception as e:
            self.logger.error(f"❌ 조건검색 실시간 알림 처리 실패: {e}")
            self.logger.error(f"조건검색 알림 처리 에러 상세: {traceback.format_exc()}") # type: ignore

    def process_market_status_data(self, data_item):
        """시장 상태 데이터 처리 (0s) - API 문서 기반"""
        try:
            # API 문서에 따른 시장 상태 데이터 처리
            values = data_item.get('values', {})
            
            # values가 딕셔너리인지 리스트인지 확인
            if isinstance(values, dict):
                # 딕셔너리 형태로 직접 처리 (실제 수신 데이터 형태)
                market_operation = values.get('215')  # 장운영구분
                execution_time = values.get('20')     # 체결시간
                remaining_time = values.get('214')    # 장시작예상잔여시간
            elif isinstance(values, list) and values:
                # 리스트 형태로 처리 (기존 방식)
                market_operation = None
                execution_time = None
                remaining_time = None
                
                for value in values:
                    if isinstance(value, dict):
                        if value.get('215'):  # 장운영구분
                            market_operation = value.get('215')
                        if value.get('20'):   # 체결시간
                            execution_time = value.get('20')
                        if value.get('214'):  # 장시작예상잔여시간
                            remaining_time = value.get('214')
            else:
                self.logger.warning(f"⚠️ 알 수 없는 시장 상태 데이터 형태: {type(values)}")
                self.logger.debug(f"📋 수신된 데이터: {data_item}")
                return
            
            # 시장 상태 저장
            self.market_status = {
                'market_operation': market_operation,
                'execution_time': execution_time,
                'remaining_time': remaining_time,
                'updated_at': datetime.now().isoformat()
            }
            
            # 시장 상태 상세 정보 로그 출력
            self.logger.info(f"🔔 장운영구분 (215): {market_operation}, 체결시간 (20): {execution_time}, 장시작예상잔여시간 (214): {remaining_time}")
            
            # 장운영구분에 따른 상세 로그 메시지
            if market_operation == '0':
                self.logger.info("🌅 KRX 장전 시간입니다.")
            elif market_operation == '2':
                self.logger.info("✅ 장마감전 동시호가 시간입니다.")
            elif market_operation == '3':
                self.logger.info("✅ KRX 장이 시작되었습니다! 거래 가능합니다.")
            elif market_operation == '8':
                self.logger.info("⏹️ 장마감 시간이 되었습니다. 거래가 종료됩니다.")
                # 장마감 시 장시작시간 구독 해제
                asyncio.create_task(self.unsubscribe_market_status())
            elif market_operation == '4':
                self.logger.info("⏸️ 장종료 예상지수종료 시간입니다.")
            elif market_operation == '8':
                self.logger.info("⏹️ 장마감 시간이 되었습니다. 거래가 종료됩니다.")
            elif market_operation == 'a':
                self.logger.info("ℹ️ 시간외 종가매매 시작")
            elif market_operation == 'P':
                self.logger.info("🔄 NXT 프리마켓이 개시되었습니다.")
            elif market_operation == 'Q':
                self.logger.info("⏸️ NXT 프리마켓이 종료되었습니다.")
            elif market_operation == 'R':
                self.logger.info("🚀 NXT 메인마켓이 개시되었습니다.")
            elif market_operation == 'S':
                self.logger.info("⏹️ NXT 메인마켓이 종료되었습니다.")
            elif market_operation == 'T':
                self.logger.info("🔄 NXT 애프터마켓 단일가가 개시되었습니다.")
            elif market_operation == 'U':
                self.logger.info("🌙 NXT 애프터마켓이 개시되었습니다.")
            elif market_operation == 'V':
                self.logger.info("⏸️ NXT 종가매매가 종료되었습니다.")
            elif market_operation == 'W':
                self.logger.info("🌙 NXT 애프터마켓이 종료되었습니다.")
            else:
                self.logger.info(f"ℹ️ 알 수 없는 장운영구분: {market_operation}")
        except Exception as e:
            self.logger.error(f"시장 상태 데이터 처리 실패: {e}", exc_info=True)
            
    
    def process_condition_search_list_response(self, response):
        """조건검색 목록조회 응답 처리"""
        try:           
            # 응답 데이터 유효성 확인
            if response is None:
                self.logger.warning("⚠️ 조건검색 목록조회 응답 데이터가 None입니다")
                return
            
            if not isinstance(response, dict):
                self.logger.warning(f"⚠️ 조건검색 목록조회 응답이 딕셔너리가 아닙니다: {type(response)}")
                return
            
            # 응답 상태 확인
            if response.get('return_code') != 0:
                self.logger.error(f"❌ 조건검색 목록조회 실패: {response.get('return_msg', '알 수 없는 오류')}")
                return
            
            # 조건검색 목록 데이터 추출
            data_list = response.get('data')
            if data_list is None:
                self.logger.warning("⚠️ 조건검색 목록 데이터가 None입니다")
                return
            
            if not isinstance(data_list, list):
                self.logger.warning(f"⚠️ 조건검색 데이터가 리스트가 아닙니다: {type(data_list)}")
                return
            
            if not data_list:
                self.logger.warning("⚠️ 등록된 조건검색이 없습니다")
                self.logger.info("💡 HTS(efriend Plus) [0110] 조건검색 화면에서 조건을 등록하고 '사용자조건 서버저장'을 클릭해주세요")
                
                # 부모 윈도우에 빈 결과 전달
                if hasattr(self, 'parent') and self.parent:
                    self.parent.condition_search_list = None
                return
            
            # 조건검색 목록 처리
            condition_search_list = []
            for item in data_list:
                if isinstance(item, list) and len(item) >= 2:
                    # 데이터 형태: ["seq", "title"]
                    condition_seq = item[0]
                    condition_name = item[1]
                    condition_search_list.append({
                        'title': condition_name,
                        'seq': condition_seq
                    })
                elif isinstance(item, dict):
                    # 딕셔너리 형태도 지원 (기존 로직)
                    condition_name = item.get('title', 'N/A')
                    condition_seq = item.get('seq', 'N/A')
                    condition_search_list.append({
                        'title': condition_name,
                        'seq': condition_seq
                    })
                else:
                    self.logger.warning(f"⚠️ 알 수 없는 데이터 형태: {item}")
            
            self.logger.debug("📋 등록된 조건검색 목록:")            
            for condition in condition_search_list:
                self.logger.debug(f"  - {condition['title']} (seq: {condition['seq']})")
            
            # 부모 윈도우에 조건검색 목록 전달
            if hasattr(self, 'parent') and self.parent:
                self.parent.condition_search_list = condition_search_list
                
                # UI 업데이트 전 로딩 플래그 설정
                if hasattr(self.parent, 'is_loading_strategy'):
                    self.parent.is_loading_strategy = True

                self.parent.trading_tab.comboStg.blockSignals(True)

                # 투자전략 콤보박스에 조건검색식 추가
                try:
                    # 기존 조건검색식 제거 (중복 방지)
                    condition_names = [condition['title'] for condition in condition_search_list]
                    for i in range(self.parent.trading_tab.comboStg.count() - 1, -1, -1):
                        item_text = self.parent.trading_tab.comboStg.itemText(i)
                        if item_text in condition_names:
                            self.parent.trading_tab.comboStg.removeItem(i)
                    
                    # 새로운 조건검색식 추가
                    added_count = 0
                    for condition in condition_search_list:
                        condition_text = condition['title']  # [조건검색] 접두사 제거
                        self.parent.trading_tab.comboStg.addItem(condition_text)
                        added_count += 1
                        self.logger.info(f"✅ 조건검색식 추가 ({added_count}/{len(condition_search_list)}): {condition_text}")
                    
                    # 저장된 조건검색식이 있는지 확인하고 자동 실행
                    self.logger.debug("🔍 저장된 조건검색식 자동 실행 확인 시작")
                    saved_condition_executed = self.parent.condition_search_manager.check_and_auto_execute_saved_condition()
                    
                    # 저장된 조건검색식이 없으면 첫 번째 조건검색 자동 실행
                    if not saved_condition_executed:
                        self.logger.info("🔍 저장된 조건검색식이 없어 첫 번째 조건검색 자동 실행")
                        if condition_search_list: # type: ignore
                            first_condition = condition_search_list[0]
                            condition_seq = first_condition['seq']
                            condition_name = first_condition['title']
                            
                            # 비동기로 조건검색 실행
                            async def auto_execute_first_condition():
                                await asyncio.sleep(2.0)  # 2초 대기
                                await self.parent.start_condition_realtime(condition_seq)
                                self.logger.info(f"✅ 첫 번째 조건검색 자동 실행 완료: {condition_name} (seq: {condition_seq})")
                            
                            asyncio.create_task(auto_execute_first_condition())
                            self.logger.info(f"🔍 첫 번째 조건검색 자동 실행 예약 (2초 후): {condition_name}")
                    
                except Exception as add_ex:
                    self.logger.error(f"투자전략 콤보박스에 조건검색식 추가 실패: {add_ex}", exc_info=True)
                finally:
                    self.parent.trading_tab.comboStg.blockSignals(False)
                    # UI 업데이트 후 로딩 플래그 해제
                    if hasattr(self.parent, 'is_loading_strategy'):
                        self.parent.is_loading_strategy = False
                        self.logger.debug("is_loading_strategy 플래그 해제")
                    
            
        except Exception as e:
            self.logger.error(f"조건검색 목록조회 응답 처리 실패: {e}", exc_info=True)
            
            
            # 오류 발생 시 부모 윈도우에 None 전달
            if hasattr(self, 'parent') and self.parent:
                self.parent.condition_search_list = None

    def process_condition_realtime_response(self, response):
        """조건검색 실시간 요청 응답 처리"""
        try:            
            # 응답 데이터 유효성 확인
            if response is None:
                self.logger.warning("⚠️ 조건검색 응답 데이터가 None입니다")
                return
            
            if not isinstance(response, dict):
                self.logger.warning(f"⚠️ 조건검색 응답이 딕셔너리가 아닙니다: {type(response)}")
                return
            
            # 응답 상태 확인
            if response.get('return_code') != 0:
                error_msg = response.get('return_msg', '알 수 없는 오류')
                seq = response.get('seq') # 실패한 seq 가져오기
                self.logger.error(f"❌ 조건검색 실시간 요청 실패 (seq={seq}): {error_msg}")

                # 실패한 경우, active_realtime_conditions에서 해당 seq 제거
                if seq and hasattr(self.parent, 'active_realtime_conditions') and seq in self.parent.active_realtime_conditions:
                    self.parent.active_realtime_conditions.discard(seq)
                    self.logger.debug(f"🗑️ 조건검색 실패로 활성 목록에서 제거: {seq} (현재: {self.parent.active_realtime_conditions})")
                return
            
            # 조건검색 결과 데이터 추출
            seq = response.get('seq') # 성공한 seq 가져오기
            if seq and hasattr(self.parent, 'active_realtime_conditions'):
                self.parent.active_realtime_conditions.add(seq)
                self.logger.debug(f"✅ 조건검색 성공으로 활성 목록에 추가: {seq} (현재: {self.parent.active_realtime_conditions})")

            data_list = response.get('data')
            if data_list is None:
                self.logger.warning("⚠️ 조건검색 데이터가 None입니다")
                return
            
            if not isinstance(data_list, list):
                self.logger.warning(f"⚠️ 조건검색 데이터가 리스트가 아닙니다: {type(data_list)}")
                return
            
            if not data_list:
                self.logger.warning("⚠️ 조건검색 실시간 요청 결과가 없습니다")
                return
            
            # 조건검색 결과 처리 (실제 데이터 구조 기반)
            stock_list = []           
            for i, item in enumerate(data_list):
                self.logger.debug(f"📋 종목 {i+1} 데이터: {item}")
                
                if isinstance(item, dict):
                    # 종목 정보 추출 (실제 데이터 필드명 사용)
                    raw_code = item.get('jmcode', '')  # 종목코드
                    
                    if raw_code:
                        # A 접두사 제거 (A004560 -> 004560)
                        clean_code = self.parent.data_manager.normalize_stock_code(raw_code) if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'data_manager') else raw_code
                        current_price = ''  # 현재가 정보 없음
                        change_rate = ''    # 등락율 정보 없음
                        
                        stock_list.append({
                            'code': clean_code,
                            'current_price': current_price,
                            'change_rate': change_rate
                        })
                    else:
                        self.logger.warning(f"⚠️ 종목코드가 비어있음: {item}")
                else:
                    self.logger.warning(f"⚠️ 종목 데이터가 딕셔너리가 아님: {type(item)} - {item}")
            
            self.logger.info(f"📊 조건검색 처리 완료: {len(stock_list)}개 종목 추출됨")
            
            if stock_list:
                self.logger.info(f"✅ 조건검색 실시간 요청 성공: {len(stock_list)}개 종목 발견")
                
                # 부모 윈도우에 조건검색 결과 전달 및 API 큐에 추가
                if hasattr(self, 'parent') and self.parent:
                    # 현재 조건검색 이름 가져오기
                    condition_name = self.parent.current_condition_name if hasattr(self.parent, 'current_condition_name') else None
                    if condition_name:
                        self.logger.info(f"🔧 조건검색 '{condition_name}'의 종목들을 API 큐에 추가 시작")
                    else:
                        self.logger.info("🔧 부모 윈도우에 API 큐 추가 시작") # type: ignore
                    
                    # 조건검색 결과를 API 큐에 추가 (차트 데이터 수집 후 모니터링에 추가됨)
                    added_count = 0
                    skipped_count = 0
                    for i, stock in enumerate(stock_list):
                        stock_code = stock['code']
                        self.logger.debug(f"📋 API 큐 추가 시도 {i+1}/{len(stock_list)}: {stock_code}")
                        
                        # 종목-조건검색 매핑 저장
                        if condition_name:
                            self.parent.stock_condition_map[stock_code] = condition_name
                            self.logger.debug(f"✅ 종목-조건검색 매핑 저장: {stock_code} → {condition_name}")
                        
                        # 이미 모니터링에 존재하는지 사전 확인
                        already_exists = False
                        if hasattr(self.parent, 'monitoringBox'):
                            for j in range(self.parent.trading_tab.monitoringBox.count()):
                                item_text = self.parent.trading_tab.monitoringBox.item(j).text()
                                existing_code = item_text.split()[0]
                                if existing_code == stock['code']:
                                    self.logger.info(f"ℹ️ 종목이 이미 모니터링에 존재하여 API 큐 추가 건너뜀: {stock['code']}")
                                    already_exists = True
                                    skipped_count += 1
                                    break
                        
                        if not already_exists:
                            # chart_cache를 통해 API 큐에 추가
                            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                                result = self.parent.chart_cache.add_stock_to_api_queue(stock['code'])
                                if result:
                                    added_count += 1
                                    self.logger.info(f"✅ API 큐 추가 성공: {stock['code']}")
                                else:
                                    # 중복이거나 이미 모니터링에 존재하는 경우
                                    self.logger.debug(f"ℹ️ API 큐 추가 건너뜀 (중복 또는 이미 존재): {stock['code']}")
                                    skipped_count += 1
                            else:
                                self.logger.error(f"❌ chart_cache가 없습니다: {stock['code']}")
                    
                    self.logger.info(f"✅ 조건검색 실시간 결과 API 큐 추가 완료: {added_count}개 종목 추가")
                   
                else:
                    self.logger.error("❌ 부모 윈도우가 없습니다")
            else:
                self.logger.warning("⚠️ 조건검색 실시간 요청 결과에 유효한 종목이 없습니다")
            
        except Exception as e:
            self.logger.error(f"❌ 조건검색 실시간 요청 응답 처리 실패: {e}")
            self.logger.error(f"조건검색 실시간 요청 응답 처리 에러 상세: {traceback.format_exc()}") # type: ignore

class KiwoomRestClient:
    """키움 REST API 클라이언트 클래스"""
    
    def __init__(self, config_file='settings.ini'):
        # 로깅 설정을 먼저 초기화
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(self.__class__.__name__)
        
        self.config_file = config_file
        self.load_config()
        
        # API 설정
        self.base_url = "https://api.kiwoom.com"  # 운영 서버
        self.mock_url = "https://mockapi.kiwoom.com"  # 모의 서버
        self.is_mock = self.config.getboolean('KIWOOM_API', 'simulation', fallback=False)  # 모의 서버 사용 여부
        
        # API 키 설정
        self.app_key = self.config.get('KIWOOM_API', 'appkey', fallback='')
        self.app_secret = self.config.get('KIWOOM_API', 'secretkey', fallback='')
        
        # 모의투자 상태 로그 출력
        if self.is_mock:
            self.logger.info("모의투자 서버 사용 모드로 설정됨")
        else:
            self.logger.info("실거래 서버 사용 모드로 설정됨")
        
        # 인증 토큰
        self.access_token = None
        self.token_expires_at = None
        self.token_file = 'kiwoom_token.json'  # 토큰 저장 파일

        # 마지막 주문 번호 저장 (부분 매도 추적용)
        self.last_order_no = None
        
        # 비동기 HTTP 클라이언트 (httpx)
        self.client = None  # 나중에 초기화 (비동기 초기화 필요)
        self._default_headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        # 계좌 정보 (주문 시 필요)
        self.account_number = self.config.get('KIWOOM_API', 'account_number', fallback='')
        self.account_product_code = self.config.get('KIWOOM_API', 'account_product_code', fallback='01')
        
        # 연결 상태
        self.is_connected = False
        self.connection_lock = Lock()
        
        # 계좌평가현황 캐시 (API 호출 빈도 제한)
        self._balance_cache = {}
        self._balance_cache_time = 0
        
        # 데이터 저장소 (REST API 전용)
        self.order_data = {}  # 주문 정보
        
        # 프로그램 시작 시 저장된 토큰 로드 시도
        self.load_saved_token()
        
    def load_config(self):
        """설정 파일 로드"""
        self.config = configparser.RawConfigParser()
        try:
            self.config.read(self.config_file, encoding='utf-8')
            self.logger.info(f"설정 파일 로드 완료: {self.config_file}")
        except Exception as e:
            self.logger.error(f"설정 파일 로드 실패: {e}", exc_info=True)
            # 기본 설정
            self.config = configparser.RawConfigParser()
            self.config.add_section('LOGIN')
            self.config.set('LOGIN', 'username', '')
            self.config.set('LOGIN', 'password', '')
            self.config.set('LOGIN', 'certpassword', '')
    
    def save_token(self):
        """토큰을 파일에 저장"""
        try:
            if not self.access_token or not self.token_expires_at:
                return
                
            token_data = {
                'access_token': self.access_token,
                'expires_at': self.token_expires_at.isoformat(),
                'is_mock': self.is_mock,
                'appkey': self.config.get('KIWOOM_API', 'appkey', fallback=''),
                'saved_at': datetime.now().isoformat()
            }
            
            with open(self.token_file, 'w', encoding='utf-8') as f:
                json.dump(token_data, f, indent=2, ensure_ascii=False)
            
            self.logger.info(f"토큰 저장 완료: {self.token_file}")
            
        except Exception as e:
            self.logger.warning(f"토큰 저장 실패: {e}", exc_info=True)
    
    def load_saved_token(self):
        """저장된 토큰을 파일에서 로드"""
        try:
            if not os.path.exists(self.token_file):
                self.logger.debug("저장된 토큰 파일이 없습니다")
                return False
            
            with open(self.token_file, 'r', encoding='utf-8') as f:
                token_data = json.load(f)
            
            # 저장된 토큰의 만료 시간 확인
            expires_at_str = token_data.get('expires_at')
            if not expires_at_str:
                self.logger.debug("토큰 파일에 만료 시간이 없습니다")
                return False
            
            expires_at = datetime.fromisoformat(expires_at_str)
            now = datetime.now()
            
            # 만료 시간이 지났는지 확인 (5분 여유)
            if expires_at <= now + timedelta(minutes=5):
                self.logger.debug(f"저장된 토큰이 만료되었습니다: {expires_at}")
                return False
            
            # 모의투자 설정이 일치하는지 확인
            saved_is_mock = token_data.get('is_mock', True)
            if saved_is_mock != self.is_mock:
                self.logger.debug(f"토큰 설정 불일치 (저장: 모의투자={saved_is_mock}, 현재: 모의투자={self.is_mock})")
                return False
            
            # appkey가 일치하는지 확인
            saved_appkey = token_data.get('appkey', '')
            current_appkey = self.config.get('KIWOOM_API', 'appkey', fallback='')
            if saved_appkey != current_appkey:
                self.logger.debug("저장된 토큰의 appkey가 현재 설정과 다릅니다")
                return False
            
            # 토큰 로드
            self.access_token = token_data.get('access_token')
            self.token_expires_at = expires_at
            
            # Authorization 헤더 설정 (클라이언트가 초기화되면 적용)
            self._default_headers['Authorization'] = f'Bearer {self.access_token}'
            
            self.logger.info(f"저장된 토큰 로드 성공 - 만료: {self.token_expires_at}")
            return True
            
        except Exception as e:
            self.logger.warning(f"토큰 로드 실패: {e}", exc_info=True)
            return False

    def clear_token(self):
        """저장 토큰/메모리 토큰 완전 폐기"""
        try:
            # 헤더에서 Authorization 제거
            try:
                if 'Authorization' in self._default_headers:
                    del self._default_headers['Authorization']
            except Exception:
                pass
            # 메모리 토큰 초기화
            self.access_token = None
            self.token_expires_at = None
            # 파일 삭제
            try:
                if os.path.exists(self.token_file):
                    os.remove(self.token_file)
                    self.logger.info(f"저장된 토큰 파일 삭제: {self.token_file}")
            except Exception as del_ex:
                self.logger.debug(f"토큰 파일 삭제 실패(무시): {del_ex}", exc_info=True)
        except Exception as ex:
            self.logger.debug(f"토큰 초기화 중 오류(무시): {ex}", exc_info=True)
    
    async def _ensure_client(self):
        """HTTP 클라이언트 초기화 (비동기)"""
        if self.client is None:
            headers = self._default_headers.copy()
            self.client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(10.0),
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)
            )
    
    async def connect(self) -> bool:
        """키움 REST API 연결 (비동기)"""
        try:
            with self.connection_lock:
                # HTTP 클라이언트 초기화
                await self._ensure_client()
                
                # 저장된 토큰이 유효한지 확인
                if self.access_token and await self.check_token_validity():
                    # 토큰이 있으면 헤더 업데이트
                    self._default_headers['Authorization'] = f'Bearer {self.access_token}'
                    if self.client:
                        self.client.headers['Authorization'] = f'Bearer {self.access_token}'
                    self.logger.debug("저장된 토큰을 사용하여 연결")
                    self.is_connected = True
                    return True
                
                # 토큰이 없거나 만료된 경우 새로 발급
                if await self.get_access_token():
                    self.is_connected = True
                    self.logger.info("키움 REST API 연결 성공")
                    return True
                else:
                    self.logger.error("키움 REST API 연결 실패")
                    return False
        except Exception as e:
            self.logger.error(f"연결 중 오류 발생: {e}", exc_info=True)
            return False
    
    async def disconnect(self):
        """키움 REST API 연결 해제 (비동기)"""
        try:
            # 중복 실행 방지
            if not hasattr(self, 'is_connected') or not self.is_connected:
                return
                
            with self.connection_lock:
                
                # 토큰 저장 (폐기하지 않음 - 재사용을 위해)
                if self.access_token:
                    # 동기 체크만 수행 (비동기 체크는 생략하여 빠른 해제)
                    token_valid = datetime.now() < (self.token_expires_at or datetime.min)
                    if token_valid:
                        try:
                            self.save_token()
                            self.logger.info("토큰 저장 완료 (재사용 가능)")
                        except Exception as token_ex:
                            self.logger.warning(f"토큰 저장 중 오류 (무시됨): {token_ex}", exc_info=True)
                
                self.is_connected = False
                
                # HTTP 클라이언트 종료
                if self.client:
                    await self.client.aclose()
                    self.client = None
                
                if hasattr(self, 'logger'):
                    self.logger.info("키움 REST API 연결 해제 완료")
                
        except Exception as e:
            if hasattr(self, 'logger'):
                self.logger.error(f"연결 해제 중 오류: {e}", exc_info=True)
            else:
                print(f"연결 해제 중 오류: {e}")
    
    async def get_access_token(self) -> bool:
        """키움 REST API 접근토큰 발급 (비동기)"""
        try:
            # HTTP 클라이언트 초기화
            await self._ensure_client()
            
            # 키움 REST API는 appkey와 secretkey를 사용
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/oauth2/token"
            
            # 인증 정보 (키움 API 문서에 따른 올바른 형식)
            auth_data = {
                "grant_type": "client_credentials",
                "appkey": self.config.get('KIWOOM_API', 'appkey', fallback=''),
                "secretkey": self.config.get('KIWOOM_API', 'secretkey', fallback='')
            }
            
            # 헤더 설정 (키움 API 문서에 따른 올바른 형식)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8'
            }
            
            # 재시도 로직 추가
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    response = await self.client.post(url, headers=headers, json=auth_data, timeout=10.0)
                    
                    if response.status_code == 200:
                        token_data = response.json()
                        
                        # 키움 API는 'token' 필드를 사용 (access_token이 아님)
                        self.access_token = token_data.get('token')
                        if not self.access_token:
                            # access_token도 시도해봄
                            self.access_token = token_data.get('access_token')
                        
                        # 만료 시간 처리 (키움 API는 expires_dt 형식 사용)
                        expires_dt = token_data.get('expires_dt')
                        if expires_dt:
                            try:
                                # expires_dt 형식: '20251018084638' (YYYYMMDDHHMMSS)
                                expires_time = datetime.strptime(expires_dt, '%Y%m%d%H%M%S')
                                self.token_expires_at = expires_time
                            except ValueError:
                                # 파싱 실패 시 기본값 사용
                                expires_in = token_data.get('expires_in', 3600)
                                self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)
                        else:
                            # expires_in 필드 사용
                            expires_in = token_data.get('expires_in', 3600)
                            self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)
                        
                        # 키움 API 응답 코드 확인
                        return_code = token_data.get('return_code')
                        if return_code != 0:
                            return_msg = token_data.get('return_msg', '알 수 없는 오류')
                            self.logger.error(f"키움 API 오류: {return_msg} (코드: {return_code})")
                            return False
                        
                        # 토큰이 제대로 설정되었는지 확인
                        if not self.access_token:
                            self.logger.error("토큰 발급 응답에서 token 또는 access_token을 찾을 수 없음")
                            self.logger.error(f"응답 데이터: {token_data}")
                            return False
                        
                        # Authorization 헤더 설정
                        self._default_headers['Authorization'] = f'Bearer {self.access_token}'
                        if self.client:
                            self.client.headers['Authorization'] = f'Bearer {self.access_token}'
                        
                        self.logger.info(f"접근토큰 발급 성공 - 토큰: {self.access_token[:10]}..., 만료: {self.token_expires_at}")
                        
                        return True
                    elif response.status_code == 500:
                        self.logger.warning(f"서버 오류 발생 (시도 {attempt + 1}/{max_retries}): {response.status_code}")
                        if attempt < max_retries - 1:
                            wait_time = (attempt + 1) * 2  # 2, 4, 6초 대기
                            self.logger.info(f"{wait_time}초 후 재시도...")
                            await asyncio.sleep(wait_time)
                            continue
                    else:
                        self.logger.error(f"토큰 발급 실패: {response.status_code}", exc_info=True)
                        self.logger.error(f"응답 헤더: {dict(response.headers)}", exc_info=True)
                        self.logger.error(f"응답 본문: {response.text}", exc_info=True)
                        return False
                        
                except (httpx.HTTPError, httpx.TimeoutException) as req_ex:
                    self.logger.warning(f"네트워크 오류 (시도 {attempt + 1}/{max_retries}): {req_ex}")
                    if attempt < max_retries - 1:
                        wait_time = (attempt + 1) * 2
                        self.logger.info(f"{wait_time}초 후 재시도...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        self.logger.error(f"네트워크 오류로 토큰 발급 실패: {req_ex}", exc_info=True)
                        return False
            
            self.logger.error(f"최대 재시도 횟수 초과로 토큰 발급 실패")
            return False
                
        except Exception as e:
            self.logger.error(f"토큰 발급 중 오류: {e}", exc_info=True)
            return False
    
    async def revoke_access_token(self) -> bool:
        """OAuth 접근토큰 폐기 (au10002) - 키움 API 문서 참고 (비동기)"""
        try:
            if not self.access_token:
                return True
            
            await self._ensure_client()
                
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/oauth2/revoke"
            
            # 키움 API 문서에 따른 요청 데이터 (appkey, secretkey, token 모두 필요)
            data = {
                "appkey": self.config.get('KIWOOM_API', 'appkey', fallback=''),
                "secretkey": self.config.get('KIWOOM_API', 'secretkey', fallback=''),
                "token": self.access_token
            }
            
            # 헤더 설정 (키움 API 문서에 따른 올바른 형식)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8'
            }
            
            self.logger.debug(f"토큰 폐기 요청: {url}")
            self.logger.debug(f"토큰 폐기 데이터: appkey={data['appkey'][:10]}..., secretkey={data['secretkey'][:10]}..., token={data['token'][:10]}...")
            
            response = await self.client.post(url, headers=headers, json=data, timeout=10.0)
            
            self.logger.debug(f"토큰 폐기 응답 코드: {response.status_code}")
            self.logger.debug(f"토큰 폐기 응답 헤더: {dict(response.headers)}")
            
            if response.status_code == 200:
                try:
                    response_data = response.json()
                    self.logger.debug(f"토큰 폐기 응답 데이터: {json.dumps(response_data, indent=2, ensure_ascii=False)}")
                    
                    # 키움 API 응답 코드 확인
                    return_code = response_data.get('return_code')
                    if return_code == 0:
                        self.logger.info("접근토큰 폐기 성공")
                        return True
                    else:
                        return_msg = response_data.get('return_msg', '알 수 없는 오류')
                        self.logger.warning(f"토큰 폐기 실패: {return_msg} (코드: {return_code})")
                        return True  # 폐기 실패해도 무시 (토큰은 자동 만료됨)
                        
                except json.JSONDecodeError:
                    self.logger.warning(f"토큰 폐기 응답 JSON 파싱 실패: {response.text}")
                    return True
                    
            elif response.status_code == 500:
                self.logger.warning(f"토큰 폐기 서버 오류 (500) - 토큰은 만료될 예정입니다")
                return True  # 서버 오류는 무시 (토큰은 자동 만료됨)
            else:
                self.logger.warning(f"토큰 폐기 실패: {response.status_code} - {response.text}", exc_info=True)
                return True  # 폐기 실패해도 무시 (토큰은 자동 만료됨)
                
        except Exception as e:
            self.logger.warning(f"토큰 폐기 중 오류 (무시됨): {e}", exc_info=True)
            return True  # 폐기 실패해도 무시 (토큰은 자동 만료됨)

    async def revoke_and_clear_token(self):
        """키움 au10002로 서버 토큰 폐기 후 로컬 토큰 완전 삭제 (비동기)"""
        try:
            await self.revoke_access_token()
        finally:
            self.clear_token()
    
    async def check_token_validity(self) -> bool:
        """토큰 유효성 검사 (비동기)"""
        if not self.access_token or not self.token_expires_at:
            self.logger.warning("토큰이 없거나 만료 시간이 설정되지 않음")
            return False
        
        # 토큰 만료 5분 전에 갱신
        if datetime.now() >= self.token_expires_at - timedelta(minutes=5):
            self.logger.info("토큰 만료 예정으로 갱신 시도")
            if await self.get_access_token():
                self.logger.info("토큰 갱신 성공")
                return True
            else:
                self.logger.error("토큰 갱신 실패", exc_info=True)
                return False
        
        return True
    
    async def get_stock_current_price(self, code: str) -> Dict:
        """주식현재가 시세 조회 (실시간 주식 정보) - 비동기
        
        Note: 키움 API에서 이 엔드포인트가 정상 작동하지 않을 수 있습니다.
        실패 시 호출한 쪽에서 추정가를 사용하도록 fallback 처리됩니다.
        """
        try:
            await self._ensure_client()
            if not await self.check_token_validity():
                return {}
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/stkinfo"
            
            # 헤더 설정
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}'
            }
            
            # 주식 정보 조회 파라미터
            params = {
                "code": code
            }
            
            response = await self.client.get(url, headers=headers, params=params, timeout=5.0)
            
            if response.status_code == 200:
                data = response.json()
                self.logger.debug(f"주식현재가 조회 응답: {json.dumps(data, indent=2, ensure_ascii=False)}")
                
                # 응답 코드 확인
                if data.get('return_code') == 0:
                    self.logger.debug(f"주식현재가 조회 성공: {code}")
                    return self._parse_stock_price_data(data)
                else:
                    return_msg = data.get('return_msg', '알 수 없는 오류')
                    self.logger.debug(f"주식현재가 조회 실패: {return_msg}")
                    return {}
            else:
                # 500 에러는 키움 API에서 지원하지 않는 엔드포인트일 가능성
                self.logger.debug(f"주식현재가 조회 실패 (서버 응답 {response.status_code}) - fallback 처리됨")
                return {}
                
        except Exception as e:
            self.logger.debug(f"주식현재가 조회 실패 ({code}): {str(e)[:50]}... - fallback 처리됨", exc_info=True)
            return {}
    
    async def get_stock_info_ka10100(self, code: str) -> Dict:
        """종목정보 조회 (ka10100) - 전일종가 포함 (비동기)"""
        try:
            await self._ensure_client()
            if not await self.check_token_validity():
                return {}
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/stkinfo"
            
            # 헤더 설정
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}',
                'api-id': 'ka10100'
            }
            
            # Body 데이터
            data = {
                'stk_cd': code
            }
            
            response = await self.client.post(url, headers=headers, json=data, timeout=10.0)
            
            if response.status_code == 200:
                result = response.json()
                self.logger.debug(f"종목정보 조회 성공 ({code}): {result.get('name', 'Unknown')}, 전일종가: {result.get('lastPrice', 'N/A')}")
                return result
            else:
                self.logger.error(f"종목정보 조회 실패 ({code}): HTTP {response.status_code}", exc_info=True)
                return {}
                
        except Exception as e:
            self.logger.error(f"종목정보 조회 중 오류 ({code}): {e}", exc_info=True)
            return {}
    
    async def get_stock_basic_info(self, code: str) -> Dict:
        """주식기본정보 조회 (ka10001) (비동기)"""
        try:
            if not await self.check_token_validity():
                return {}
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/stkinfo"
            
            params = {
                "code": code,
                "info_type": "basic"
            }
            
            await self._ensure_client()
            response = await self.client.get(url, headers=self._default_headers, params=params, timeout=10.0)
            
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"주식기본정보 조회 실패: {response.status_code}", exc_info=True)
                return {}
                
        except Exception as e:
            self.logger.error(f"주식기본정보 조회 중 오류: {e}", exc_info=True)
            return {}
    
    async def get_stock_chart_data(self, code: str, period: str = "1m") -> pd.DataFrame:
        """주식 차트 데이터 조회 (비동기)"""
        try:
            if not await self.check_token_validity():
                return pd.DataFrame()
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/chart"
            
            params = {
                "code": code,
                "period": period
            }
            
            await self._ensure_client()
            response = await self.client.get(url, headers=self._default_headers, params=params, timeout=10.0)
            
            if response.status_code == 200:
                data = response.json()
                return self._parse_chart_data(data)
            else:
                self.logger.error(f"차트 데이터 조회 실패: {response.status_code}", exc_info=True)
                return pd.DataFrame()
                
        except Exception as e:
            self.logger.error(f"차트 데이터 조회 중 오류: {e}", exc_info=True)
            return pd.DataFrame()
    
    async def get_stock_tic_chart(self, code: str, tic_scope: int = 30, cont_yn: str = 'N', next_key: str = '') -> Dict:
        """주식 틱 차트 데이터 조회 (ka10079) - 참고 코드 기반 개선 (비동기)"""
        try:
            await self._ensure_client()
            if not await self.check_token_validity():
                return {}
            
            # API 요청 제한 확인 및 대기
            await ApiLimitManager.check_api_limit_and_wait_async("틱 차트 조회", request_type="tic_chart")
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/chart"
            
            # ka10079 요청 데이터 (참고 코드와 동일한 구조)
            data = {
                "stk_cd": code,                    # 종목코드
                "tic_scope": str(tic_scope),       # 틱범위: 1,3,5,10,30
                "upd_stkpc_tp": "1"                # 수정주가구분: 0 or 1
            }
            
            # 헤더 데이터 (참고 코드와 동일한 구조)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',  # 컨텐츠타입
                'authorization': f'Bearer {self.access_token}',    # 접근토큰
                'cont-yn': cont_yn,                                # 연속조회여부
                'next-key': next_key,                              # 연속조회키
                'api-id': 'ka10079'                                # TR명
            }
            
            self.logger.debug(f"틱 차트 API 호출: {code}, 틱범위: {tic_scope}, 연속조회: {cont_yn}")
            
            # HTTP POST 요청
            response = await self.client.post(url, headers=headers, json=data, timeout=10.0)
            
            # 응답 상태 코드 확인
            if response.status_code == 200:
                response_data = response.json()
                self.logger.debug(f"틱 차트 API 응답 성공: {code}")
                
                # 틱 차트 데이터 파싱
                tic_data = self._parse_tic_chart_data(response_data)
                
                # 체결강도 데이터는 제거됨 (ka10046 API 사용 안함)
                # 체결강도 데이터가 없으면 기본값 0.0으로 설정
                if 'strength' not in tic_data or not tic_data['strength']:
                    tic_data['strength'] = [0.0] * len(tic_data.get('close', []))
                
                return tic_data
            else:
                self.logger.error(f"틱 차트 데이터 조회 실패: {response.status_code}", exc_info=True)
                try:
                    error_data = response.json()
                    self.logger.error(f"오류 상세: {error_data}", exc_info=True)
                except:
                    self.logger.error(f"응답 내용: {response.text}", exc_info=True)
                return {}
                
        except Exception as e:
            self.logger.error(f"틱 차트 데이터 조회 중 오류: {e}", exc_info=True)
            return {}
    
    
    async def get_stock_minute_chart(self, code: str, period: int = 3) -> Dict:
        """주식 분봉 차트 데이터 조회 (ka10080) (비동기)"""
        try:
            await self._ensure_client()
            if not await self.check_token_validity():
                return {}
            
            # API 요청 제한 확인 및 대기
            await ApiLimitManager.check_api_limit_and_wait_async("분봉 차트 조회", request_type="minute_chart")
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/chart"
            
            # ka10080 요청 데이터 (분봉 차트)
            data = {
                "stk_cd": code,
                "tic_scope": str(period),  # 1:1분, 3:3분, 5:5분, 10:10분, 15:15분, 30:30분, 45:45분, 60:60분
                "upd_stkpc_tp": "1"
            }
            
            # 헤더 설정 (ka10080 기준)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}',
                'cont-yn': 'N',
                'next-key': '',
                'api-id': 'ka10080'
            }
            
            response = await self.client.post(url, headers=headers, json=data, timeout=10.0)
            
            if response.status_code == 200:
                response_data = response.json()
                return self._parse_minute_chart_data(response_data)
            else:
                self.logger.error(f"분봉 차트 데이터 조회 실패: {response.status_code}", exc_info=True)
                return {}
                
        except Exception as e:
            self.logger.error(f"분봉 차트 데이터 조회 중 오류: {e}", exc_info=True)
            return {}
    
    async def get_deposit_detail(self) -> Dict:
        """예수금상세현황요청 (kt00001) - 키움 REST API (비동기)
        예수금, 출금가능금액, 주문가능금액 등을 조회합니다.
        """
        try:
            await self._ensure_client()
            if not await self.check_token_validity():
                return {}
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/acnt"
            
            # 헤더 설정 (키움 API 문서 참고)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}',
                'cont-yn': 'N',  # 연속조회여부
                'next-key': '',  # 연속조회키
                'api-id': 'kt00001',  # TR명
            }
            
            # 요청 데이터 (키움 API 문서 참고)
            params = {
                'qry_tp': '3',  # 3: 추정조회, 2: 일반조회
            }
            
            # POST 요청 (키움 API 문서에 따라 POST 사용)
            response = await self.client.post(url, headers=headers, json=params, timeout=10.0)
            
            if response.status_code == 200:
                data = response.json()
                
                # return_code가 '0'이거나 없으면 성공으로 처리 (키움 API는 return_code 또는 rt_cd를 사용)
                return_code = data.get('return_code')
                return_msg = data.get('return_msg', '')
                
                # 성공 조건: return_code가 '0'이거나 None이거나, 또는 특정 성공 메시지가 있는 경우
                success_conditions = [
                    str(return_code) == '0',
                    return_code is None
                ]
                
                if any(success_conditions) or '조회' in return_msg:
                    self.logger.debug("예수금상세현황 조회 성공")
                    return data
                else:
                    self.logger.error(f"예수금상세현황 조회 실패: {return_msg}", exc_info=True)
                    return {}
            else:
                self.logger.error(f"예수금상세현황 조회 실패: {response.status_code}", exc_info=True)
                return {}
                
        except Exception as e:
            self.logger.error(f"예수금상세현황 조회 중 오류: {e}", exc_info=True)
            return {}
    
    async def get_acnt_balance(self) -> Dict:
        """계좌평가현황 조회 (kt00004) - 키움 REST API (비동기)
        주의: 이 메서드는 REST API를 통한 일회성 조회입니다.
        실시간 잔고 데이터는 KiwoomWebSocketClient에서 처리됩니다.
        """
        # 캐시 유효성 확인 (5초 이내면 캐시 사용)
        current_time = time.time()
        cache_validity_period = 5  # 5초
        
        if hasattr(self, '_balance_cache_time') and (current_time - self._balance_cache_time) < cache_validity_period:
            if self._balance_cache:
                return self._balance_cache

        try:
            await self._ensure_client()
            if not await self.check_token_validity():
                return {}
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/acnt"
            
            # 헤더 설정 (키움 API 문서 참고)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}',
                'cont-yn': 'N',  # 연속조회여부
                'next-key': '',  # 연속조회키
                'api-id': 'kt00004',  # TR명
            }
            
            # 요청 데이터 (키움 API 문서 참고)
            params = {
                'qry_tp': '0',  # 상장폐지조회구분 0:전체, 1:상장폐지종목제외
                'dmst_stex_tp': 'KRX',  # 국내거래소구분 KRX:한국거래소,NXT:넥스트트레이드
            }
            
            # POST 요청 (키움 API 문서에 따라 POST 사용)
            response = await self.client.post(url, headers=headers, json=params, timeout=10.0)
            
            
            if response.status_code == 200:
                data = response.json()

                # 응답 코드 확인 (return_code가 0이면 성공)
                if str(data.get('return_code', '')) == '0':
                    # 캐시 업데이트
                    self._balance_cache = data
                    self._balance_cache_time = current_time
                    return data
                else:
                    return_msg = data.get('return_msg', '알 수 없는 오류')
                    self.logger.error(f"계좌평가현황 조회 실패: {return_msg}", exc_info=True)
                    # API 실패 시 기존 캐시 데이터 반환 (있다면)
                    return self._balance_cache if self._balance_cache else {}
            else:
                self.logger.error(f"계좌평가현황 조회 실패: {response.status_code}", exc_info=True)
                # API 실패 시 기존 캐시 데이터 반환 (있다면)
                return self._balance_cache if self._balance_cache else {}
                
        except Exception as e:
            self.logger.error(f"계좌평가현황 조회 중 오류: {e}", exc_info=True)
            # API 실패 시 기존 캐시 데이터 반환 (있다면)
            return self._balance_cache if self._balance_cache else {}

    
    async def place_buy_order(self, code: str, quantity: int, price: int = 0, order_type: str = "market") -> bool:
        """매수 주문 (키움 REST API 기반) - 시장가만 지원 (비동기)
        
        신 REST API (kt10000) 방식 사용
        """
        try:
            await self._ensure_client()
            if not await self.check_token_validity():
                return False
            
            # API 요청 제한 확인 및 대기 (주문 전용)
            await ApiLimitManager.check_api_limit_and_wait_async("매수 주문", request_type="order")
            
            # API URL 설정
            host = 'https://mockapi.kiwoom.com' if self.is_mock else 'https://api.kiwoom.com'
            endpoint = '/api/dostk/ordr'
            url = host + endpoint
            
            # 시장가 주문으로 강제 설정
            ord_uv = ''  # 시장가는 주문단가 빈 문자열
            trde_tp = '3'  # 매매구분: 3=시장가
            
            self.logger.debug(f"매수 주문: {code} {quantity}주 (시장가)")
            
            # 헤더 설정 (키움증권 공식 예시 참고)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}',
                'cont-yn': 'N',  # 연속조회여부
                'next-key': '',  # 연속조회키
                'api-id': 'kt10000',  # TR명
            }
            
            # 요청 데이터 (키움증권 공식 예시 참고)
            data = {
                'dmst_stex_tp': 'KRX',  # 국내거래소구분: KRX, NXT, SOR
                'stk_cd': code,         # 종목코드
                'ord_qty': str(quantity),  # 주문수량
                'ord_uv': ord_uv,       # 주문단가 (시장가는 빈 문자열)
                'trde_tp': trde_tp,     # 매매구분: 3=시장가
                'cond_uv': '',          # 조건단가
            }
            
            # HTTP POST 요청
            try:
                response = await self.client.post(url, headers=headers, json=data, timeout=10.0)
                
                # 응답 처리
                if response.status_code == 200:
                    result = response.json()
                    
                    # 응답 상태 확인
                    if result.get('return_code') == 0:
                        ord_no = result.get('ord_no', '')
                        self.last_order_no = ord_no # 마지막 주문 번호 저장
                        self.logger.info(f"✅ 매수 주문 성공: {code} {quantity}주 (주문번호: {ord_no})")
                        return True
                    else:
                        error_msg = result.get('return_msg', 'Unknown error')
                        self.logger.error(f"매수 주문 실패: {error_msg}", exc_info=True)
                        # 종료 계좌(RC4091) 대응: 토큰 폐기 후 재인증 유도
                        if 'RC4091' in error_msg or '종료된 계좌' in error_msg:
                            try:
                                self.logger.warning("⚠️ 종료된 계좌 감지(RC4091) - 자동매매 일시 중지 및 토큰 재발급 절차 시작")
                                # 자동매매 중지
                                try:
                                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'objat') and self.parent.objat:
                                        self.parent.objat.stop_auto_trading()
                                except Exception:
                                    pass
                                # 서버 토큰 폐기 후 로컬 토큰 삭제
                                await self.revoke_and_clear_token()
                            except Exception:
                                pass
                        return False
                else:
                    self.logger.error(f"매수 주문 실패: HTTP {response.status_code}", exc_info=True)
                    return False
                    
            except (httpx.HTTPError, httpx.TimeoutException) as req_ex:
                self.logger.error(f"HTTP 요청 실패: {req_ex}", exc_info=True)
                return False
                
        except Exception as e:
            self.logger.error(f"매수 주문 중 오류: {e}", exc_info=True)
            
            return False
    
    async def place_sell_order(self, code: str, quantity: int, price: int = 0, order_type: str = "market") -> bool:
        """매도 주문 (키움 REST API 기반) - 시장가만 지원 (비동기)
        
        신 REST API (kt10001) 방식 사용
        """
        try:
            await self._ensure_client()
            if not await self.check_token_validity():
                return False
            
            # API 요청 제한 확인 및 대기 (주문 전용)
            await ApiLimitManager.check_api_limit_and_wait_async("매도 주문", request_type="order")
            
            # 보유 수량 체크는 호출자(sell_item)에서 이미 수행했으므로 생략
            # (REST API 호출 횟수 절약 및 중복 체크 제거)
            
            # API URL 설정
            host = 'https://mockapi.kiwoom.com' if self.is_mock else 'https://api.kiwoom.com'
            endpoint = '/api/dostk/ordr'
            url = host + endpoint
            
            # 시장가 주문으로 강제 설정
            ord_uv = ''  # 시장가는 주문단가 빈 문자열
            trde_tp = '3'  # 매매구분: 3=시장가           
            
            # 헤더 설정 (키움증권 공식 예시 참고)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}',
                'cont-yn': 'N',  # 연속조회여부
                'next-key': '',  # 연속조회키
                'api-id': 'kt10001',  # TR명 (매도주문)
            }
            
            # 요청 데이터 (키움증권 공식 예시 참고)
            data = {
                'dmst_stex_tp': 'KRX',  # 국내거래소구분: KRX, NXT, SOR
                'stk_cd': code,         # 종목코드
                'ord_qty': str(quantity),  # 주문수량
                'ord_uv': ord_uv,       # 주문단가 (시장가는 빈 문자열)
                'trde_tp': trde_tp,     # 매매구분: 3=시장가
                'cond_uv': '',          # 조건단가
            }
            
            # HTTP POST 요청
            try:
                response = await self.client.post(url, headers=headers, json=data, timeout=10.0)
                
                # 응답 처리
                if response.status_code == 200:
                    result = response.json()
                    
                    # 응답 상태 확인
                    if result.get('return_code') == 0:
                        ord_no = result.get('ord_no', '')
                        self.last_order_no = ord_no # 마지막 주문 번호 저장
                        self.logger.debug(f"✅ 매도 주문 성공: {code} {quantity}주 (주문번호: {ord_no})")
                        return True
                    else:
                        error_msg = result.get('return_msg', 'Unknown error')
                        return_code = result.get('return_code', 0)
                        
                        self.logger.error(f"❌ 매도 주문 실패: {error_msg}")
                        
                        # "매도가능수량 부족" 에러인 경우 상세 정보 추가
                        if '800033' in error_msg or '매도가능수량' in error_msg or '매도가능' in error_msg:
                            self.logger.error(f"🔍 [{code}] 주문 요청 수량: {quantity}주 (주문가능수량 부족 - 다른 주문 처리 중일 수 있음)")
                        # 종료 계좌(RC4091) 대응: 자동 정지 + 토큰 폐기
                        if 'RC4091' in error_msg or '종료된 계좌' in error_msg:
                            try:
                                self.logger.warning("⚠️ 종료된 계좌 감지(RC4091) - 자동매매 일시 중지 및 토큰 재발급 절차 시작")
                                try:
                                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'objat') and self.parent.objat:
                                        self.parent.objat.stop_auto_trading()
                                except Exception:
                                    pass
                                await self.revoke_and_clear_token()
                            except Exception:
                                pass
                        
                        return False
                else:
                    self.logger.error(f"매도 주문 실패: HTTP {response.status_code}", exc_info=True)
                    return False
                    
            except (httpx.HTTPError, httpx.TimeoutException) as req_ex:
                self.logger.error(f"HTTP 요청 실패: {req_ex}", exc_info=True)
                return False
                
        except Exception as e:
            self.logger.error(f"매도 주문 중 오류: {e}", exc_info=True)
            
            return False
    
    async def get_order_history(self) -> List[Dict]:
        """주문 내역 조회 (비동기)"""
        try:
            if not await self.check_token_validity():
                return []
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/ordr"
            
            await self._ensure_client()
            response = await self.client.get(url, headers=self._default_headers, timeout=10.0)
            
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"주문 내역 조회 실패: {response.status_code}", exc_info=True)
                return []
                
        except Exception as e:
            self.logger.error(f"주문 내역 조회 중 오류: {e}", exc_info=True)
            return []    

    async def send_slack_notification_on_sell(self, prev_balance_info, daily_realized_profit, daily_realized_profit_rate, sold_qty=None):
        """매도 체결 시 슬랙 알림 전송"""
        try:
            # Slack 설정 로드
            # [SLACK] 또는 [slack] 섹션 모두 지원
            if self.config.has_section('SLACK'):
                slack_webhook_url = self.config.get('SLACK', 'webhook', fallback=None)
            else:
                slack_webhook_url = self.config.get('slack', 'webhook', fallback=None)
            if not slack_webhook_url:
                self.logger.debug("Slack 웹훅 URL이 설정되지 않아 알림을 보내지 않습니다.")
                return

            stock_name = prev_balance_info.get('name', '알수없음')
            stock_code = prev_balance_info.get('code', '000000')
            
            # 알림 제목 설정
            if sold_qty:
                title = f"📈 부분 매도 완료: {stock_name}({stock_code})"
                fallback_text = f"부분 매도: {stock_name} ({sold_qty}주)"
            else:
                title = f"✅ 전량 매도 완료: {stock_name}({stock_code})"
                fallback_text = f"전량 매도: {stock_name}"

            # 메시지 포맷
            profit_text = f"*{daily_realized_profit:+,}원* ({daily_realized_profit_rate:+.2f}%)"
            color = "#28a745" if daily_realized_profit >= 0 else "#dc3545" # 수익: 녹색, 손실: 빨강
            
            message = {
                "text": title, # 모바일 알림 등에서 기본으로 표시될 텍스트
                "attachments": [
                    {
                        "color": color,
                        "fallback": f"{fallback_text} - 당일실현손익: {daily_realized_profit:+,}원",
                        "fields": [
                            {"title": "체결 수량", "value": f"{sold_qty}주" if sold_qty else "전량", "short": True},
                            {"title": "당일실현손익", "value": profit_text, "short": True}
                        ],
                    }
                ]
            }
            
            # 비동기 HTTP 클라이언트로 웹훅 전송
            async with httpx.AsyncClient() as client:
                await client.post(slack_webhook_url, json=message)
            self.logger.info(f"🔔 슬랙 알림 전송 완료: {title}")
        except Exception as e:
            self.logger.error(f"Slack 알림 전송 실패: {e}", exc_info=True)
            
    
    def _parse_stock_price_data(self, data: Dict) -> Dict:
        """주식 가격 데이터 파싱"""
        try:
            return {
                'code': data.get('code', ''),
                'name': data.get('name', ''),
                'current_price': data.get('current_price', 0),
                'change': data.get('change', 0),
                'change_rate': data.get('change_rate', 0),
                'volume': data.get('volume', 0),
                'high': data.get('high', 0),
                'low': data.get('low', 0),
                'open': data.get('open', 0),
                'previous_close': data.get('previous_close', 0),
                'market_cap': data.get('market_cap', 0),
                'per': data.get('per', 0),
                'pbr': data.get('pbr', 0)
            }
        except Exception as e:
            self.logger.error(f"주식 가격 데이터 파싱 오류: {e}", exc_info=True)
            return {}
    
    def _parse_chart_data(self, data: Dict) -> pd.DataFrame:
        """차트 데이터 파싱"""
        try:
            if 'data' not in data:
                return pd.DataFrame()
            
            df = pd.DataFrame(data['data'])
            
            # 컬럼명 표준화
            column_mapping = {
                'timestamp': 'datetime',
                'open_price': 'open',
                'high_price': 'high',
                'low_price': 'low',
                'close_price': 'close',
                'volume': 'volume'
            }
            
            df = df.rename(columns=column_mapping)
            
            # datetime 컬럼 변환
            if 'datetime' in df.columns:
                df['datetime'] = pd.to_datetime(df['datetime'])
                df.set_index('datetime', inplace=True)
            
            return df
            
        except Exception as e:
            self.logger.error(f"차트 데이터 파싱 오류: {e}", exc_info=True)
            return pd.DataFrame()
    
    def _parse_tic_chart_data(self, data: Dict) -> Dict:
        """틱 차트 데이터 파싱 (ka10079 응답 형식) - 키움 API 문서 참고"""
        try:
            # API 응답 구조 확인
            if 'return_code' in data and data['return_code'] != 0:
                return_msg = data.get('return_msg', '알 수 없는 오류')
                self.logger.error(f"API 응답 오류: {return_msg}")
                return {}
            
            # stk_tic_chart_qry 필드에서 데이터 추출 (키움 API 문서 참고)
            if 'stk_tic_chart_qry' not in data:
                self.logger.warning("stk_tic_chart_qry 필드가 응답에 없습니다")
                return {}
            
            tic_data = data['stk_tic_chart_qry']
            if not tic_data:
                self.logger.warning("틱 차트 데이터가 비어있습니다")
                return {}
            
            # 필요한 필드 추출 (체결강도 필드 추가)
            parsed_data = {
                'time': [],
                'open': [],
                'high': [],
                'low': [],
                'close': [],
                'volume': [],
                'strength': [],  # 체결강도 필드 추가
                'last_tic_cnt': []
            }
            
            # 디버깅: 원본 데이터 시간 순서 확인
            if tic_data:
                original_first = tic_data[0].get('cntr_tm', '')
                original_last = tic_data[-1].get('cntr_tm', '')
                self.logger.debug(f"틱 원본 데이터: 총 {len(tic_data)}개, 첫번째={original_first}, 마지막={original_last}")
                
                # 원본 데이터 구조 디버깅 (첫 번째 항목)
                if tic_data:
                    first_item = tic_data[0]
                    
                    # 시간 관련 필드들 확인
                    time_fields = ['cntr_tm', 'time', 'timestamp', 'dt', 'date_time']
                    for field in time_fields:
                        if field in first_item:
                            self.logger.debug(f"시간 필드 '{field}': {first_item[field]}")
            
            # 시간 순서를 정상적으로 정렬 (오래된 시간부터 최신 시간 순서)
            def get_sort_key(item):
                # 여러 시간 필드 시도
                time_fields = ['cntr_tm', 'time', 'timestamp', 'dt', 'date_time', 'created_at']
                for field in time_fields:
                    if item.get(field):
                        return str(item.get(field))
                return ''
            
            tic_data.sort(key=get_sort_key)
            
            # 모든 데이터 처리 (정렬 후)
            data_to_process = tic_data
            
            # 디버깅: 시간 순서 확인
            if data_to_process:
                first_time = get_sort_key(data_to_process[0])
                last_time = get_sort_key(data_to_process[-1])
                self.logger.debug(f"틱 데이터 시간 순서 (정렬 후): 총 {len(data_to_process)}개, 첫번째={first_time}, 마지막={last_time}")
            
            for item in data_to_process:
                # 시간 정보 (여러 필드명 시도)
                time_str = ''
                time_fields = ['cntr_tm', 'time', 'timestamp', 'dt', 'date_time', 'created_at']
                for field in time_fields:
                    if item.get(field):
                        time_str = str(item.get(field))
                        break
                
                if time_str:
                    try:
                        # 체결시간 형식에 따라 파싱 (HHMMSS 또는 YYYYMMDDHHMMSS)
                        if len(time_str) == 6:  # HHMMSS
                            # 현재 날짜와 결합
                            today = datetime.now().strftime('%Y%m%d')
                            full_time = f"{today}{time_str}"
                            dt = datetime.strptime(full_time, '%Y%m%d%H%M%S')
                        elif len(time_str) == 14:  # YYYYMMDDHHMMSS
                            dt = datetime.strptime(time_str, '%Y%m%d%H%M%S')
                        elif len(time_str) == 8:  # YYYYMMDD
                            dt = datetime.strptime(time_str, '%Y%m%d')
                        else:
                            dt = datetime.now()
                        parsed_data['time'].append(dt)
                    except Exception as parse_ex:
                        self.logger.warning(f"시간 파싱 실패: {time_str}, {parse_ex}", exc_info=True)
                        parsed_data['time'].append(datetime.now())
                else:
                    parsed_data['time'].append(datetime.now())
                
                # OHLCV 데이터 (API 문서에 따른 정확한 필드명 사용)
                # API 문서: open_pric, high_pric, low_pric, cur_prc, trde_qty
                
                # 원본 데이터 로깅 (디버깅용)
                raw_open = item.get('open_pric', '')
                raw_high = item.get('high_pric', '')
                raw_low = item.get('low_pric', '')
                raw_close = item.get('cur_prc', '')
                raw_volume = item.get('trde_qty', '')
                
                # 공통 함수 사용
                open_price = abs(safe_float_conversion(raw_open))
                high_price = abs(safe_float_conversion(raw_high))
                low_price = abs(safe_float_conversion(raw_low))
                close_price = abs(safe_float_conversion(raw_close))
                volume = int(abs(safe_float_conversion(raw_volume, 0)))  # 음수면 양수로 전환
                
                # OHLC 논리 검증
                if not (low_price <= min(open_price, close_price) and max(open_price, close_price) <= high_price):
                    self.logger.warning(f"틱 OHLC 논리 오류: O={open_price}, H={high_price}, L={low_price}, C={close_price}")
                    high_price = max(open_price, high_price, low_price, close_price)
                    low_price = min(open_price, high_price, low_price, close_price)
                
                # 필드가 비어있거나 0인 경우 현재가로 대체
                if open_price == 0:
                    open_price = close_price
                if high_price == 0:
                    high_price = close_price
                if low_price == 0:
                    low_price = close_price
                
                parsed_data['open'].append(open_price)
                parsed_data['high'].append(high_price)
                parsed_data['low'].append(low_price)
                parsed_data['close'].append(close_price)
                parsed_data['volume'].append(volume)
                
                # 체결강도 데이터는 제거됨 (ka10046 API 사용 안함)
                # 기본값 0.0으로 설정
                parsed_data['strength'].append(0.0)
                
                # 마지막틱갯수 (last_tic_cnt) 필드 추가
                last_tic_cnt = item.get('last_tic_cnt', '')
                parsed_data['last_tic_cnt'].append(last_tic_cnt)
            
            # 틱 차트 데이터 파싱 완료 로그
            self.logger.debug(f"틱 차트 데이터 파싱 완료: {len(parsed_data['close'])}개 데이터")
            
            return parsed_data
            
        except Exception as e:
            self.logger.error(f"틱 차트 데이터 파싱 오류: {e}", exc_info=True)
            return {}
    
    
    
    def _parse_minute_chart_data(self, data: Dict) -> Dict:
        """분봉 차트 데이터 파싱 (ka10080 응답 형식) - 키움 API 문서 참고"""
        try:
            # API 응답 구조 확인
            if 'return_code' in data and data['return_code'] != 0:
                return_msg = data.get('return_msg', '알 수 없는 오류')
                self.logger.error(f"API 응답 오류: {return_msg}")
                return {}
            
            # 분봉 차트 데이터 필드명 확인 (ka10080은 'stk_min_pole_chart_qry' 필드 사용)
            if 'stk_min_pole_chart_qry' not in data:
                self.logger.warning("분봉 차트 데이터 필드가 응답에 없습니다")
                return {}
            
            minute_data = data['stk_min_pole_chart_qry']
            if not minute_data:
                self.logger.warning("분봉 차트 데이터가 비어있습니다")
                return {}
            
            # 필요한 필드 추출
            parsed_data = {
                'time': [],
                'open': [],
                'high': [],
                'low': [],
                'close': [],
                'volume': []
            }
            
            # 디버깅: 원본 데이터 시간 순서 확인
            if minute_data:
                original_first = minute_data[0].get('cntr_tm', '')
                original_last = minute_data[-1].get('cntr_tm', '')
                self.logger.debug(f"분봉 원본 데이터: 총 {len(minute_data)}개, 첫번째={original_first}, 마지막={original_last}")
            
            # 시간 순서를 정상적으로 정렬 (오래된 시간부터 최신 시간 순서)
            minute_data.sort(key=lambda x: x.get('cntr_tm', ''))
            
            # 모든 데이터 처리 (정렬 후)
            data_to_process = minute_data
            
            # 디버깅: 시간 순서 확인
            if data_to_process:
                first_time = data_to_process[0].get('cntr_tm', '')
                last_time = data_to_process[-1].get('cntr_tm', '')
                self.logger.debug(f"분봉 데이터 시간 순서 (정렬 후): 총 {len(data_to_process)}개, 첫번째={first_time}, 마지막={last_time}")
            
            for item in data_to_process:
                # 시간 정보 (분봉 차트 시간 형식) - API 문서에 따르면 'cntr_tm' 필드 사용
                time_str = item.get('cntr_tm', '')
                if time_str:
                    try:
                        # 분봉 차트 시간 형식 파싱 (YYYYMMDDHHMMSS)
                        if len(time_str) == 14:  # YYYYMMDDHHMMSS
                            dt = datetime.strptime(time_str, '%Y%m%d%H%M%S')
                        elif len(time_str) == 12:  # YYYYMMDDHHMM
                            dt = datetime.strptime(time_str, '%Y%m%d%H%M')
                        elif len(time_str) == 8:  # YYYYMMDD
                            dt = datetime.strptime(time_str, '%Y%m%d')
                        else:
                            dt = datetime.now()
                        parsed_data['time'].append(dt)
                    except Exception as parse_ex:
                        self.logger.warning(f"분봉 시간 파싱 실패: {time_str}, {parse_ex}", exc_info=True)
                        parsed_data['time'].append(datetime.now())
                else:
                    parsed_data['time'].append(datetime.now())
                
                # OHLCV 데이터 (API 문서에 따른 정확한 필드명 사용)
                # API 문서: open_pric, high_pric, low_pric, cur_prc, trde_qty
                
                # 원본 데이터 로깅 (디버깅용)
                raw_open = item.get('open_pric', '')
                raw_high = item.get('high_pric', '')
                raw_low = item.get('low_pric', '')
                raw_close = item.get('cur_prc', '')
                raw_volume = item.get('trde_qty', '')
                
                # 공통 함수 사용
                open_price = abs(safe_float_conversion(raw_open))
                high_price = abs(safe_float_conversion(raw_high))
                low_price = abs(safe_float_conversion(raw_low))
                close_price = abs(safe_float_conversion(raw_close))
                volume = int(abs(safe_float_conversion(raw_volume, 0)))  # 음수면 양수로 전환
                
                # OHLC 논리 검증
                if not (low_price <= min(open_price, close_price) and max(open_price, close_price) <= high_price):
                    self.logger.warning(f"분봉 OHLC 논리 오류: O={open_price}, H={high_price}, L={low_price}, C={close_price}")
                
                parsed_data['open'].append(open_price)
                parsed_data['high'].append(high_price)
                parsed_data['low'].append(low_price)
                parsed_data['close'].append(close_price)
                parsed_data['volume'].append(volume)
            
            self.logger.debug(f"분봉 차트 데이터 파싱 완료: {len(parsed_data['close'])}개 데이터")
            return parsed_data
            
        except Exception as e:
            self.logger.error(f"분봉 차트 데이터 파싱 오류: {e}", exc_info=True)
            return {}
    
    def is_market_open(self) -> bool:
        """시장 개장 여부 확인"""
        try:
            # 시장 상태 조회 실패 시 시간대 기반 판단
            now = datetime.now()
            current_time = now.time()
            
            # 평일 09:00 ~ 15:30 (장중 시간)
            market_start = dt_time(9, 0)
            market_end = dt_time(15, 30)
            
            # 평일이고 장중 시간이면 개장으로 판단
            if now.weekday() < 5 and market_start <= current_time <= market_end:
                return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"시장 개장 확인 중 오류: {e}", exc_info=True)
    
    def __del__(self):
        """소멸자 - 연결 해제 (중복 실행 방지)"""
        try:
            # 이미 연결이 해제되었는지 확인
            # __del__에서는 await를 사용할 수 없으므로 asyncio.create_task 사용 불가
            # 여기서는 연결 상태만 확인하고 실제 해제는 다른 곳에서 처리
            if hasattr(self, 'is_connected') and self.is_connected:
                # __del__에서는 비동기 호출 불가능하므로 플래그만 설정
                self.is_connected = False
        except Exception:
            # logger가 없거나 다른 오류가 발생해도 무시
            pass

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