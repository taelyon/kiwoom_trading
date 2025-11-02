"""
키움 REST API 기반 자동매매 프로그램
크레온 플러스 API를 키움 REST API로 전면 리팩토링
"""

# 표준 라이브러리
import asyncio
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
import requests
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
                
    except Exception as ex:
        print(f"로깅 설정 실패: {ex}")

# ==================== API 제한 관리 ====================
class ApiLimitManager:
    """API 제한 관리 클래스 (개선된 버전)"""
    
    # API 요청 간격 관리 (초 단위)
    _last_request_time = {}
    _request_intervals = {
        'tic_chart': 1.5,    # 틱 차트: 1.5초 간격 (429 에러 방지)
        'minute_chart': 1.5,  # 분봉 차트: 1.5초 간격 (429 에러 방지)
        'tic': 0.5,          # 틱 데이터: 0.5초 간격
        'minute': 0.5,        # 분봉 데이터: 0.5초 간격
        'default': 0.5        # 기본: 0.5초 간격
    }
    
    @classmethod
    def check_api_limit_and_wait(cls, operation_name="API 요청", rqtype=0, request_type=None):
        """API 제한 확인 및 대기 (개선된 버전)"""
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
                # API 간격 조정 로그 제거 (너무 빈번함)
                # 실제 대기 시간 적용 (스레드에서 실행되므로 안전)
                time.sleep(wait_time)
            
            # 요청 시간 업데이트
            cls._last_request_time[request_type] = time.time()
            return True
            
        except Exception as ex:
            logging.error(f"API 제한 확인 중 오류: {ex}")
            return False
    
    @classmethod
    def _get_request_type(cls, operation_name):
        """요청 타입 결정"""
        if '틱' in operation_name or 'tic' in operation_name.lower():
            return 'tic_chart'
        elif '분봉' in operation_name or 'minute' in operation_name.lower():
            return 'minute_chart'
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
            logging.error(f"API 제한 확인 중 오류: {ex}")
            return False
    
    @classmethod
    def reset_request_times(cls):
        """요청 시간 기록 초기화"""
        cls._last_request_time.clear()
        # 초기화 로그 제거 (불필요)

# ==================== 로그 핸들러 ====================
class QTextEditLogger(logging.Handler):
    """QTextEdit에 로그를 출력하는 핸들러 (스레드 안전)"""
    
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
                
        except Exception as ex:
            # 로그 핸들러에서 예외가 발생하면 무시 (무한 루프 방지)
            pass

# ==================== 데이터베이스 관리 ====================
class AsyncDatabaseManager:
    """비동기 데이터베이스 관리 클래스 (I/O 바운드 작업)"""
    
    def __init__(self, db_path="stock_data.db"):
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
            
            async with aiosqlite.connect(self.db_path) as conn:
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
            logging.error(f"데이터베이스 초기화 실패: {ex}")
            raise ex
    
    async def save_stock_data(self, code, tic_data, min_data):
        """통합 주식 데이터 저장 (틱봉 기준, 분봉 데이터 포함)"""
        try:
            if not tic_data or not min_data:
                return
            
            async with aiosqlite.connect(self.db_path) as conn:
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
                
                # 모든 지표 통합 (중복 제거)
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
                            logging.debug(f"틱봉 지표 처리 중 오류 ({indicator}): {ex}")
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
                            logging.debug(f"분봉 지표 처리 중 오류 ({indicator}): {ex}")
                            values.append(None)
                    
                    values.append(current_time)

                    await cursor.execute(sql, tuple(values))
                
                await conn.commit()
                # 데이터 저장 완료 로그 제거 (너무 빈번함)
                
        except Exception as ex:
            logging.error(f"통합 주식 데이터 저장 실패 ({code}): {ex}")
            logging.error(f"상세 오류: {traceback.format_exc()}")
    
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
                    logging.debug(f"📊 새 컬럼 추가: {col}")
                except Exception as e:
                    # 컬럼이 이미 존재하는 경우 무시
                    if "duplicate column name" not in str(e).lower():
                        logging.warning(f"⚠️ 컬럼 추가 실패 ({col}): {e}")
                        
        except Exception as ex:
            logging.error(f"❌ 테이블 스키마 확인/업데이트 실패: {ex}")
    
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
            logging.error(f"분봉 데이터 매칭 실패: {ex}")
            return -1
    
    async def save_trade_record(self, code, datetime_str, order_type, quantity, price, strategy=""):
        """매매 기록 저장 (비동기 I/O)"""
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.cursor()
                
                amount = quantity * price
                
                await cursor.execute('''
                    INSERT INTO trade_records 
                    (code, datetime, order_type, quantity, price, amount, strategy)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (code, datetime_str, order_type, quantity, price, amount, strategy))
                
                await conn.commit()
                
                logging.debug(f"매매 기록 저장: {code} {order_type} {quantity}주 @ {price}")
            
        except Exception as ex:
            logging.error(f"매매 기록 저장 실패: {ex}")
            logging.error(f"상세 에러:\n{traceback.format_exc()}")
    

# ==================== 키움 트레이더 클래스 ====================
class KiwoomTrader(QObject):
    """키움 REST API 기반 트레이더 클래스"""
    
    # 시그널 정의
    signal_log = pyqtSignal(str)
    signal_update_balance = pyqtSignal(dict)
    signal_order_result = pyqtSignal(str, str, int, float, bool)  # code, order_type, quantity, price, success
    
    def __init__(self, client, buycount, parent=None):
        super().__init__()
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
        
        # 설정 로드
        self.load_settings()

        logging.debug(f"키움 트레이더 초기화 완료 (목표 매수 종목 수: {self.buycount})")
    
    def _init_database_async(self):
        """비동기 데이터베이스 초기화 트리거"""
        try:
            # qasync 환경에서 메인 이벤트 루프 사용 시도
            try:
                loop = asyncio.get_running_loop()
                # 이미 실행 중인 이벤트 루프가 있으면 태스크로 실행
                asyncio.create_task(self.db_manager.init_database())
                logging.debug("✅ DB 초기화를 비동기 태스크로 시작")
                return
            except RuntimeError:
                # 실행 중인 이벤트 루프가 없음 - ThreadPoolExecutor로 처리
                logging.debug("⚠️ 실행 중인 이벤트 루프가 없어 ThreadPoolExecutor 사용")
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
                    logging.error(f"비동기 데이터베이스 초기화 실행 오류: {e}")
                    return None
            
            # 별도 스레드에서 비동기 초기화 실행
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_async_init)
                future.result(timeout=30)  # 30초 타임아웃
                
        except Exception as ex:
            logging.error(f"비동기 데이터베이스 초기화 트리거 실패: {ex}")
    
    def load_settings(self):
        """설정 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # 매매 설정
            self.evaluation_interval = config.getint('TRADING', 'evaluation_interval', fallback=5)
            
            # 데이터 저장 설정
            self.data_saving_interval = config.getint('DATA_SAVING', 'interval_seconds', fallback=5)
            
            # 차트 업데이트 설정
            self.chartdata_update_interval = config.getint('CHART', 'chartdata_update_interval', fallback=10)
            
            logging.debug("설정 로드 완료")
            
        except Exception as ex:
            logging.error(f"설정 로드 실패: {ex}")
    
    def update_balance(self):
        """잔고 정보 업데이트"""
        try:
            balance_data = self.client.get_acnt_balance()
            if balance_data:
                self.signal_update_balance.emit(balance_data)
                
                # 보유 종목 정보 업데이트
                holdings = balance_data.get('holdings', {})
                for code, info in holdings.items():
                    self.holdings[code] = info
                    
        except Exception as ex:
            logging.error(f"잔고 업데이트 실패: {ex}")
    
    def get_current_price(self, code):
        """현재가 조회 (실패 시 0 반환하여 fallback 처리)"""
        try:
            price_data = self.client.get_stock_current_price(code)
            return price_data.get('current_price', 0)
        except Exception as ex:
            logging.debug(f"현재가 조회 실패 ({code}) - fallback 처리됨")
            return 0
    
    def place_buy_order(self, code, quantity, price=0, strategy=""):
        """매수 주문 (키움 REST API 기반)"""
        try:
            # 1. 보유 종목 확인 (이미 보유 중인 종목은 매수 제외)
            if self.parent and hasattr(self.parent, 'boughtBox'):
                for i in range(self.parent.boughtBox.count()):
                    item_code = self.parent.boughtBox.item(i).text()
                    if item_code == code:
                        logging.info(f"⚠️ 매수 주문 취소: {code}는 이미 보유 중인 종목입니다.")
                        return False
            
            # 2. 최대 보유 종목 수 확인
            if self.parent and hasattr(self.parent, 'login_handler'):
                max_count = self.parent.login_handler.get_target_buy_count()
                current_count = self.parent.login_handler.get_current_holdings_count()
                available_buy_count = self.parent.login_handler.get_available_buy_count()
                
                if available_buy_count <= 0:
                    logging.warning(f"⚠️ 매수 주문 취소: 최대 보유 종목 수 도달 ({code})")
                    logging.warning(f"   현황: 최대 {max_count}종목, 현재 {current_count}종목, 가능 {available_buy_count}종목")
                    return False
                else:
                    # 매수 주문 진행 중 상태로 설정
                    self.pending_buy_orders.add(code)
                    logging.debug(f"⏳ [{code}] 매수 주문 진행 중 상태로 설정 (중복 주문 방지)")

                    logging.debug(f"✅ 매수 가능 확인: {code} (현재 {current_count}/{max_count}종목, 가능 {available_buy_count}종목)")
            
            # 키움 REST API를 통한 매수 주문
            success = self.client.place_buy_order(code, quantity, price)
            
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
                    logging.warning("⚠️ 이벤트 루프가 없어 매수 기록 저장을 건너뜁니다")
                
                # 포트폴리오 업데이트
                self.buy_prices[code] = price if price > 0 else self.get_current_price(code)
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

                logging.debug(f"✅ holdings 업데이트: {code} (수량: {quantity}주, 평단: {self.buy_prices[code]:,}원)")
                
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
                        logging.debug(f"✅ 보유종목 리스트에 추가: {code} (총 {new_count}개 종목 보유)")
                
                self.signal_order_result.emit(code, "buy", quantity, price, True)
                logging.debug(f"✅ 매수 주문 성공: {code} {quantity}주 (키움 REST API)")
                return True
            else:
                self.signal_order_result.emit(code, "buy", quantity, price, False)
                # 주문 실패 시, '매수 주문 진행 중' 상태 해제
                if code in self.pending_buy_orders:
                    self.pending_buy_orders.discard(code)
                    logging.info(f"🟢 [{code}] 매수 주문 실패로 진행 중 상태 해제")
                logging.error(f"❌ 매수 주문 실패: {code}")
                return False
                
        except Exception as ex:
            logging.error(f"❌ 매수 주문 중 오류 ({code}): {ex}")
            if code in self.pending_buy_orders:
                self.pending_buy_orders.discard(code)
                logging.info(f"🟢 [{code}] 매수 주문 오류로 진행 중 상태 해제")
            self.signal_order_result.emit(code, "buy", quantity, price, False)
            return False
    
    def place_sell_order(self, code, quantity, price=0, strategy=""):
        """매도 주문 (키움 REST API 기반)"""
        try:
            # quantity가 0 이하인 경우 주문을 실행하지 않음
            if quantity <= 0:
                logging.warning(f"⚠️ 매도 주문 수량이 0 이하이므로 주문을 실행하지 않습니다: {code}, 수량: {quantity}")
                return False

            # 키움 REST API를 통한 매도 주문
            # 주문 전, '주문 진행 중' 상태로 설정
            self.pending_sell_orders.add(code)
            logging.info(f"⏳ [{code}] 매도 주문 진행 중 상태로 설정 (중복 주문 방지)")

            success = self.client.place_sell_order(code, quantity, price)
            
            if success:
                # 매도 기록 저장 (비동기 태스크로 실행)
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                sell_price = price if price > 0 else self.get_current_price(code)
                # qasync 환경에서 안전하게 태스크 생성
                try:
                    asyncio.get_running_loop()  # 루프 확인
                    asyncio.create_task(self.db_manager.save_trade_record(code, current_time, "sell", quantity, sell_price, strategy))
                except RuntimeError:
                    logging.warning("⚠️ 이벤트 루프가 없어 매도 기록 저장을 건너뜁니다")
                
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
                        logging.debug(f"⚠️ {code} 보유 수량 정보 없음 - 전량 매도로 처리")
                
                # 전량 매도 시 최고가 정보 초기화
                if is_full_sell and code in self.highest_prices:
                            del self.highest_prices[code]
                            logging.debug(f"🗑️ {code} 최고가 정보 초기화 (전량 매도)")

                # 부분 매도 주문 정보 추적
                ord_no = self.client.last_order_no
                if ord_no:
                    self.sell_order_details[ord_no] = {
                        'code': code,
                        'total_qty': quantity,
                        'filled_qty': 0
                    }
                    logging.info(f"📋 부분 매도 주문 추적 시작: 주문번호={ord_no}, 종목={code}, 수량={quantity}주")
                
                # holdings 딕셔너리 업데이트 (매도 평가를 위한 동기화)
                if is_full_sell:
                    # 전량 매도 시 holdings에서 제거
                    if code in self.holdings:
                        del self.holdings[code]
                        logging.debug(f"✅ holdings에서 제거: {code} (전량 매도)")
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
                        logging.debug(f"✅ holdings 수량 업데이트: {code} ({remaining_qty}주 → {new_quantity}주)")
                
                # 전량 매도 시 보유종목 리스트에서 즉시 제거 (종목 수 제한 동기화)
                if is_full_sell and self.parent and hasattr(self.parent, 'boughtBox'):
                    for i in range(self.parent.boughtBox.count()):
                        if self.parent.boughtBox.item(i).text() == code:
                            self.parent.boughtBox.takeItem(i)
                            new_count = self.parent.boughtBox.count()
                            logging.info(f"✅ 보유종목 리스트에서 제거: {code} (전량 매도, 남은 종목 {new_count}개)")
                            break
                elif not is_full_sell:
                    logging.debug(f"ℹ️ {code} 부분 매도 (보유: {remaining_qty}주, 매도: {quantity}주)")
                
                self.signal_order_result.emit(code, "sell", quantity, price, True)
                logging.debug(f"✅ 매도 주문 성공: {code} {quantity}주 (키움 REST API)")
                return True
            else:
                self.signal_order_result.emit(code, "sell", quantity, price, False)
                # 주문 실패 시, '주문 진행 중' 상태 해제
                if code in self.pending_sell_orders:
                    self.pending_sell_orders.discard(code)
                    logging.info(f"🟢 [{code}] 매도 주문 실패로 진행 중 상태 해제")
                logging.error(f"❌ 매도 주문 실패: {code}")
                return False
                
        except Exception as ex:
            logging.error(f"❌ 매도 주문 중 오류 ({code}): {ex}")
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
                            logging.debug(f"🗑️ [{code}] get_portfolio_status에서 제거 (웹소켓 balance_data에 없음)")
                        
                        # 웹소켓 balance_data의 종목들을 처리
                        for code, balance_info in ws_balance_data.items():
                            quantity = balance_info.get('quantity', 0)
                            order_available_qty = balance_info.get('order_available_qty', 0)
                            
                            # 수량이 0인 경우 holdings에서 제거 (전량 매도 완료)
                            if quantity == 0:
                                if code in merged_holdings:
                                    del merged_holdings[code]
                                    logging.debug(f"🗑️ [{code}] get_portfolio_status에서 제거 (웹소켓 수량=0)")
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
                                        logging.debug(f"🔄 [{code}] 부분 매도 후 buy_price 복원: {self.buy_prices[code]:,.0f}원")
                                    if code not in merged_buy_times and code in self.buy_times:
                                        merged_buy_times[code] = self.buy_times[code]
                                        logging.debug(f"🔄 [{code}] 부분 매도 후 buy_time 복원")

                                    if ws_quantity != holdings_quantity:
                                        # 웹소켓 수량으로 업데이트
                                        merged_holdings[code]['quantity'] = ws_quantity
                                        # 매입단가도 업데이트 (없거나 0인 경우)
                                        if code not in merged_buy_prices or merged_buy_prices[code] == 0:
                                            merged_buy_prices[code] = balance_info.get('average_price', 0)
            except Exception as ws_ex:
                # 웹소켓 동기화 실패해도 계속 진행 (경고만 출력)
                logging.debug(f"⚠️ 웹소켓 balance_data 동기화 중 오류 (무시): {ws_ex}")
            
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
            logging.error(f"포트폴리오 상태 조회 실패: {ex}")
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
                    logging.debug("🗑️ 웹소켓 잔고가 비어있어 self.holdings를 초기화합니다.")
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
                logging.debug(f"🗑️ [{code}] holdings 동기화: 웹소켓에 없어 제거됨")

            # 웹소켓에 있는 종목은 self.holdings에 추가/업데이트
            for code, balance_info in ws_balance_data.items():
                quantity = balance_info.get('quantity', 0)
                if quantity > 0:
                    if code not in self.holdings:
                        self.holdings[code] = {'quantity': quantity}
                        self.buy_prices[code] = balance_info.get('average_price', 0)
                        self.buy_times[code] = datetime.now() # 시간 정보가 없으므로 현재 시간으로 설정
                        logging.debug(f"🆕 [{code}] holdings 동기화: 웹소켓 잔고로 신규 추가")
                    else:
                        self.holdings[code]['quantity'] = quantity # 수량 동기화
                else:
                    # 수량이 0인 경우 (전량 매도 완료) holdings에서 제거
                    if code in self.holdings:
                        del self.holdings[code]
                        logging.debug(f"🗑️ [{code}] holdings 동기화: 웹소켓 수량=0으로 제거됨")
                    if code in self.buy_prices:
                        del self.buy_prices[code]
                    if code in self.buy_times:
                        del self.buy_times[code]
                    if code in self.highest_prices:
                        del self.highest_prices[code]
        except Exception as ex:
            logging.warning(f"⚠️ self.holdings와 웹소켓 잔고 동기화 실패: {ex}")
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

    def get_execution_data(self):
        """웹소켓 실시간 체결 데이터 조회"""
        if not hasattr(self, 'execution_data'):
            self.execution_data = {}
        return self.execution_data.copy()

    def get_account_balance(self) -> Dict:
        """투자계좌자산현황조회 - 투자가능 현금 조회용
        매수 시 투자가능 현금을 확인하기 위한 API
        """
        try:
            if not self.client.check_token_validity():
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
            
            # POST 요청
            response = requests.post(url, headers=headers, json=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                # 응답 코드 확인
                if data.get('rt_cd') == '0':
                    logging.debug("투자계좌자산현황조회 성공")
                    return data
                else:
                    return_msg = data.get('msg1', '알 수 없는 오류')
                    logging.error(f"투자계좌자산현황조회 실패: {return_msg}")
                    return {}
            else:
                logging.error(f"투자계좌자산현황조회 실패: {response.status_code}")
                logging.error(f"응답: {response.text}")
                return {}
                
        except Exception as e:
            logging.error(f"투자계좌자산현황조회 중 오류: {e}")
            return {}

    def get_available_cash(self) -> float:
        """투자가능 현금 조회
        매수 시 사용할 수 있는 현금 금액을 반환
        (캐싱을 통해 API 호출 빈도를 제한하여 429 오류 방지)
        """
        try:
            # 캐시 유효성 확인 (5초 이내면 캐시 사용)
            current_time = time.time()
            cache_validity_period = 5  # 5초
            
            if hasattr(self, '_cash_cache_time') and (current_time - self._cash_cache_time) < cache_validity_period:
                return self._cash_cache
            
            deposit_data = self.client.get_deposit_detail()
            if not deposit_data:
                return self._cash_cache  # 캐시된 값 반환
            
            # 주문가능금액 조회 (투자가능 현금)
            available_cash = float(deposit_data.get('ord_alow_amt', 0))
            
            # 캐시 업데이트
            self._cash_cache = available_cash
            self._cash_cache_time = current_time
            
            logging.debug(f"투자가능 현금: {available_cash:,.0f}원 (캐시: {cache_validity_period}초)")
            return available_cash
            
        except Exception as e:
            logging.error(f"투자가능 현금 조회 중 오류: {e}")
            return self._cash_cache if hasattr(self, '_cash_cache') else 0.0

# ==================== 키움 전략 클래스 ====================
class KiwoomStrategy(QObject):
    """키움 REST API 기반 전략 클래스"""
    
    # 시그널 정의
    signal_strategy_result = pyqtSignal(str, str, dict)  # code, action, data
    clear_signal = pyqtSignal()
    
    def __init__(self, trader, parent):
        super().__init__()
        self.trader = trader
        self.client = trader.client
        self.db_manager = trader.db_manager
        self.parent = parent
        
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
                            self.strategy_config[strategy_name] = dict(config.items(strategy_name))
                            logging.debug(f"✅ 전략 설정 로드: {strategy_name}")
            
            logging.debug(f"전략 설정 로드 완료: {self.current_strategy}")
            
        except Exception as ex:
            logging.error(f"전략 설정 로드 실패: {ex}")
    
    def evaluate_strategy(self, code, market_data):
        """전략 평가 및 실행"""
        try:
            # 디버그 로그: 최초 1회만 출력 (종목별)
            if not hasattr(self, '_eval_debug_codes'):
                self._eval_debug_codes = set()
            
            is_first_eval = code not in self._eval_debug_codes
            if is_first_eval:
                logging.debug(f"📊 [{code}] 전략 평가 시작 (전략: {self.current_strategy})")
                self._eval_debug_codes.add(code)
            
            # 현재 전략에 따른 매수/매도 신호 평가
            # 1. 통합 전략이 선택된 경우: 모든 종목에 통합 전략 적용
            # 2. 조건검색으로 찾은 종목: 해당 조건검색의 전략 사용
            # 3. 그 외: 현재 선택된 전략 사용
            
            stock_condition_map = self.parent.stock_condition_map if hasattr(self.parent, 'stock_condition_map') else {}
            
            # 1순위: 조건검색으로 포착된 종목은 해당 조건검색 전략을 사용
            if code in stock_condition_map:
                strategy_name = stock_condition_map[code]
                if is_first_eval:
                    logging.debug(f"📍 [{code}] 조건검색 전략 사용: {strategy_name}")
            # 2순위: 그 외의 경우(수동 추가 등)는 UI에서 선택된 전략을 사용
            else:
                strategy_name = self.current_strategy
                if is_first_eval:
                    logging.debug(f"📍 [{code}] UI 선택 전략 사용: {strategy_name}")
            
            if strategy_name != "통합 전략" and strategy_name not in self.strategy_config:
                if is_first_eval:
                    logging.warning(f"⚠️ [{code}] 전략 '{strategy_name}'이 설정에 없음")
                return
            
            if is_first_eval:
                logging.debug(f"✅ [{code}] 전략 설정 확인됨: {strategy_name}")
            
            # 매수 신호 평가
            buy_signals = self.get_buy_signals(code, market_data, strategy_name)
            if buy_signals:
                logging.debug(f"📈 [{code}] 매수 신호 {len(buy_signals)}개 발견")
                self.execute_buy_signals(code, buy_signals)
            elif is_first_eval:
                logging.debug(f"ℹ️ [{code}] 매수 조건 미충족")
            
            # 매도 신호 평가 (보유 종목인 경우에만)
            portfolio = self.trader.get_portfolio_status()
            if code in portfolio['holdings']:
                if is_first_eval:
                    logging.debug(f"🔎 [{code}] 보유 종목 - 매도 평가 진행")
                sell_signals = self.get_sell_signals(code, market_data, strategy_name)
                if sell_signals:
                    logging.info(f"📉 [{code}] 매도 신호 {len(sell_signals)}개 발견")
                    self.execute_sell_signals(code, sell_signals)
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
                                    logging.warning(f"⚠️ [{code}] 웹소켓에는 보유 중이지만 self.holdings에 없음 (웹소켓 수량: {ws_quantity}주)")
                                    logging.warning(f"⚠️ [{code}] get_portfolio_status 동기화 필요 - holdings: {list(portfolio['holdings'].keys())}, 웹소켓: {list(ws_balance_data.keys())}")
                    except Exception as ws_check_ex:
                        logging.debug(f"⚠️ [{code}] 웹소켓 체크 중 오류: {ws_check_ex}")
                    
                    if not ws_has_stock:
                        logging.debug(f"ℹ️ [{code}] 보유 종목 아님 - 매도 평가 건너뜀")
                    
        except Exception as ex:
            logging.error(f"❌ [{code}] 전략 평가 실패: {ex}")
            logging.error(f"상세 에러: {traceback.format_exc()}")
    
    def get_buy_signals(self, code, market_data, strategy_name):
        """매수 신호 생성 - strategy_utils를 사용한 기술적 지표 기반 평가"""
        try:
            signals = []
            
            # 디버그 로그: 최초 1회만 출력 (종목별)
            if not hasattr(self, '_buy_signal_debug_codes'):
                self._buy_signal_debug_codes = set()
            
            is_first_check = code not in self._buy_signal_debug_codes
            if is_first_check:
                logging.debug(f"🔍 [{code}] 매수 신호 검사 시작")
                self._buy_signal_debug_codes.add(code)
            
            # 포트폴리오 상태 확인
            portfolio = self.trader.get_portfolio_status()
            if portfolio['total_holdings'] >= portfolio['max_holdings']:
                if is_first_check:
                    logging.debug(f"⚠️ [{code}] 매수 불가: 보유 종목 수 한도 도달 ({portfolio['total_holdings']}/{portfolio['max_holdings']})")
                return signals
            
            # 이미 보유 중인 종목인지 확인
            if code in portfolio['holdings']:
                if is_first_check:
                    logging.debug(f"⚠️ [{code}] 매수 불가: 이미 보유 중")
                return signals

            # '매수 주문 진행 중'인 종목은 매수 신호 생성 건너뛰기
            if code in self.trader.pending_buy_orders:
                if is_first_check:
                    logging.debug(f"⏳ [{code}] 매수 주문이 이미 진행 중이므로 신호 생성을 건너뜁니다.")
                return signals
            
            # 차트 데이터 가져오기 (틱/분봉) - chart_cache에서 직접 가져오기
            chart_data = pd.DataFrame()
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                # chart_cache.get_cached_data()를 사용하여 tic_data 가져오기
                cache_data = self.parent.chart_cache.get_cached_data(code)
                if cache_data:
                    tic_data = cache_data.get('tic_data', {})
                    if tic_data and len(tic_data.get('close', [])) > 0:
                        # 데이터 타입을 명시적으로 float로 변환 (talib 에러 방지)
                        try:
                            # OHLCV 기본 데이터
                            df_dict = {
                                'time': tic_data.get('time', []),
                                'open': pd.to_numeric(tic_data.get('open', []), errors='coerce'),
                                'high': pd.to_numeric(tic_data.get('high', []), errors='coerce'),
                                'low': pd.to_numeric(tic_data.get('low', []), errors='coerce'),
                                'close': pd.to_numeric(tic_data.get('close', []), errors='coerce'),
                                'volume': pd.to_numeric(tic_data.get('volume', []), errors='coerce')
                            }
                            
                            # 캐시된 기술적 지표도 포함 (있는 경우)
                            indicator_keys = [
                                'MA5', 'MA10', 'MA20', 'MA50', 'MA60', 'MA120',
                                'RSI', 'MACD', 'MACD_SIGNAL', 'MACD_HIST',
                                'STOCH_K', 'STOCH_D', 'WILLIAMS_R', 'ROC', 'OBV', 'OBV_MA20',
                                'BB_UPPER', 'BB_MIDDLE', 'BB_LOWER', 'ATR'
                            ]
                            
                            indicators_included = 0
                            for key in indicator_keys:
                                if key in tic_data and tic_data[key] is not None:
                                    indicator_data = tic_data[key]
                                    # 길이가 OHLCV와 같은지 확인
                                    if hasattr(indicator_data, '__len__') and len(indicator_data) == len(df_dict['close']):
                                        df_dict[key] = indicator_data
                                        indicators_included += 1
                            
                            chart_data = pd.DataFrame(df_dict)
                            
                            # NaN 제거 (OHLCV만, 지표는 NaN 허용)
                            chart_data = chart_data.dropna(subset=['open', 'high', 'low', 'close', 'volume'])
                            
                            if is_first_check:
                                logging.debug(f"✅ [{code}] 차트 데이터 준비 완료: {len(chart_data)}개 틱 (캐시된 지표 {indicators_included}개 포함)")
                        except Exception as ex:
                            if is_first_check:
                                logging.warning(f"⚠️ [{code}] 차트 데이터 변환 실패: {ex}")
                            chart_data = pd.DataFrame()
                    else:
                        if is_first_check:
                            logging.warning(f"⚠️ [{code}] tic_data가 비어있음")
                else:
                    if is_first_check:
                        logging.warning(f"⚠️ [{code}] cache_data가 없음")
            else:
                if is_first_check:
                    logging.warning(f"⚠️ [{code}] chart_cache 없음")
            
            # 매수 전략 로드
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
                            if is_first_check:
                                logging.warning(f"⚠️ [{code}] 매수 전략 파싱 실패: {section_name}.{key}")

                if buy_strategies and is_first_check:
                    logging.debug(f"✅ [{code}] 통합 전략 로드 완료: 급등주+갭상승 매수 전략 {len(buy_strategies)}개")
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
                            if is_first_check:
                                logging.warning(f"⚠️ [{code}] 매수 전략 파싱 실패: {key}")
                    if buy_strategies and is_first_check:
                        logging.debug(f"✅ [{code}] strategy_config에서 매수 전략 {len(buy_strategies)}개 로드됨: {strategy_name}")
            
            # 전략이 없으면 기본 전략 사용 (매우 보수적)
            if not buy_strategies:
                if is_first_check:
                    logging.warning(f"⚠️ [{code}] 매수 전략 없음 - 기본 전략 사용 (RSI < 30 + MACD 골든크로스)")
                # 기본 전략: RSI 과매도 + MACD 골든크로스
                buy_strategies = [{
                    'name': '기본 전략',
                    'conditions': [
                        {'indicator': 'RSI', 'operator': '<', 'value': 30},
                        {'indicator': 'MACD_HIST', 'operator': '>', 'value': 0}
                    ]
                }]
            
            if is_first_check:
                logging.debug(f"✅ [{code}] 최종 매수 전략 {len(buy_strategies)}개 준비 완료")
            
            # strategy_utils를 사용하여 매수 전략 평가
            condition_met, matched_strategy = strategy_utils.evaluate_buy_strategies(
                code=code,
                strategies=buy_strategies,
                kiwoom_data=market_data,
                chart_data=chart_data,
                portfolio_info=portfolio
            )
            
            if is_first_check:
                if condition_met and matched_strategy:
                    logging.debug(f"✅ [{code}] 매수 조건 충족: {matched_strategy.get('name', 'unknown')}")
                else:
                    logging.debug(f"ℹ️ [{code}] 매수 조건 미충족")
            
            if condition_met and matched_strategy:
                current_price = market_data.get('current_price', 0)
                
                # 매수 수량 계산 (최대투자종목수 기반 분산투자)
                available_cash = self.trader.get_available_cash() if hasattr(self, 'trader') else 0
                
                # 가용자금이 0 이하이거나 현재가가 0이면 매수 신호 생성 안함
                if available_cash <= 0:
                    if is_first_check:
                        logging.debug(f"ℹ️ [{code}] 가용자금 부족으로 매수 불가 ({available_cash:,.0f}원)")
                    return []
                
                if current_price <= 0:
                    if is_first_check:
                        logging.debug(f"ℹ️ [{code}] 현재가 정보 없음 - 매수 불가")
                    return []
                
                # 매수가능 종목수 조회
                if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'login_handler'):
                    available_buy_count = self.parent.login_handler.get_available_buy_count()
                else:
                    available_buy_count = portfolio.get('max_holdings', 3) - portfolio.get('total_holdings', 0)
                
                # 매수 가능 종목수가 0 이하면 매수 신호 생성 안함
                if available_buy_count <= 0:
                    if is_first_check:
                        logging.debug(f"ℹ️ [{code}] 최대 보유 종목수 도달 - 매수 불가")
                    return []
                
                # 한 종목당 투자 예산 = 가용자금 ÷ 매수가능종목수
                budget = available_cash // available_buy_count
                quantity = max(1, int(budget / current_price))
                
                strategy_display_name = matched_strategy.get('name', strategy_name)
                logging.info(f"📈 매수 신호 발생: {code} - {strategy_display_name}")
                logging.debug(f"💰 매수 수량 계산: 가용자금={available_cash:,.0f}원, 매수가능종목={available_buy_count}개")
                logging.debug(f"   종목당예산={budget:,.0f}원, 현재가={current_price:,}원 → {quantity}주")
                
                signals.append({
                    'strategy': matched_strategy.get('name', strategy_name),
                    'code': code,
                    'quantity': quantity,
                    'price': 0,  # 시장가
                    'reason': f"기술적 지표 기반 매수 조건 충족: {matched_strategy.get('name', '')}"
                })
            
            return signals
            
        except Exception as ex:
            logging.error(f"매수 신호 생성 실패 ({code}): {ex}")
            traceback.print_exc()
            return []
    
    def get_sell_signals(self, code, market_data, strategy_name):
        """매도 신호 생성 - strategy_utils를 사용한 기술적 지표 기반 평가"""
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

            # '주문 진행 중'인 종목은 매도 신호 생성 건너뛰기
            if code in self.trader.pending_sell_orders:
                if is_first_sell_check:
                    logging.debug(f"⏳ [{code}] 매도 주문이 이미 진행 중이므로 신호 생성을 건너뜁니다.")
                return signals
            
            # 최초 매도 평가 시작 로그
            if is_first_sell_check:
                logging.debug(f"🔍 [{code}] 매도 평가 시작 (전략: {strategy_name})")
            
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
                    if is_first_sell_check:
                        logging.debug(f"⏳ [{code}] 매수 후 {time_since_buy:.1f}초 경과. 매도 평가 유예 중 (최소 {min_hold_seconds}초)")
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
                    logging.info(f"📈 {code} 최고가 갱신: {old_highest:,}원 → {current_price:,}원")
                
                # 포트폴리오 딕셔너리에 업데이트된 최고가 반영
                portfolio['highest_prices'] = self.trader.highest_prices.copy()
            
            # 이동 손절 (Trailing Stop) 로직 추가
            # 예: 고점 대비 2.5% 하락 시 매도
            trailing_stop_pct = -2.5 
            highest_price = self.trader.highest_prices.get(code, 0)
            if highest_price > buy_price: # 매수가보다 오른 경우에만 작동
                from_peak_pct = (current_price - highest_price) / highest_price * 100 if highest_price > 0 else 0
                if from_peak_pct < trailing_stop_pct:
                    logging.info(f"📉 이동 손절 신호: {code} (고점: {highest_price:,.0f}원, 현재가: {current_price:,.0f}원, {from_peak_pct:.2f}%)")
                    signals.append({
                        'strategy': f'이동 손절 ({trailing_stop_pct}%)',
                        'code': code,
                        'quantity': quantity,
                        'price': 0, # 시장가
                        'reason': f"고점({highest_price:,.0f}원) 대비 {trailing_stop_pct}% 하락"
                    })
                    return signals # 이동 손절 조건 만족 시 다른 매도 조건보다 우선하여 즉시 반환

            # 차트 데이터 가져오기 (틱/분봉) - chart_cache에서 직접 가져오기
            chart_data = pd.DataFrame()
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                cache_data = self.parent.chart_cache.get_cached_data(code)
                if cache_data:
                    tic_data = cache_data.get('tic_data', {})
                    if tic_data and len(tic_data.get('close', [])) > 0:
                        # 데이터 타입을 명시적으로 float로 변환 (talib 에러 방지)
                        try:
                            # OHLCV 기본 데이터
                            df_dict = {
                                'time': tic_data.get('time', []),
                                'open': pd.to_numeric(tic_data.get('open', []), errors='coerce'),
                                'high': pd.to_numeric(tic_data.get('high', []), errors='coerce'),
                                'low': pd.to_numeric(tic_data.get('low', []), errors='coerce'),
                                'close': pd.to_numeric(tic_data.get('close', []), errors='coerce'),
                                'volume': pd.to_numeric(tic_data.get('volume', []), errors='coerce')
                            }
                            
                            # 캐시된 기술적 지표도 포함 (있는 경우)
                            indicator_keys = [
                                'MA5', 'MA10', 'MA20', 'MA50', 'MA60', 'MA120',
                                'RSI', 'MACD', 'MACD_SIGNAL', 'MACD_HIST',
                                'STOCH_K', 'STOCH_D', 'WILLIAMS_R', 'ROC', 'OBV', 'OBV_MA20',
                                'BB_UPPER', 'BB_MIDDLE', 'BB_LOWER', 'ATR'
                            ]
                            
                            for key in indicator_keys:
                                if key in tic_data and tic_data[key] is not None:
                                    indicator_data = tic_data[key]
                                    # 길이가 OHLCV와 같은지 확인
                                    if hasattr(indicator_data, '__len__') and len(indicator_data) == len(df_dict['close']):
                                        df_dict[key] = indicator_data
                            
                            chart_data = pd.DataFrame(df_dict)
                            
                            # NaN 제거 (OHLCV만, 지표는 NaN 허용)
                            chart_data = chart_data.dropna(subset=['open', 'high', 'low', 'close', 'volume'])
                        except Exception as ex:
                            logging.debug(f"⚠️ [{code}] 매도 차트 데이터 변환 실패: {ex}")
                            chart_data = pd.DataFrame()
            
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
                            logging.debug(f"⚠️ [{code}] 매도 전략 파싱 실패: {section_name}.{key}")
                if is_first_sell_check and sell_strategies:
                    logging.debug(f"✅ [{code}] 통합 전략 로드 완료: 급등주+갭상승 매도 전략 {len(sell_strategies)}개")
            
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
                        logging.debug(f"⚠️ [{code}] 매도 전략 파싱 실패: {key}")
            
            # 전략이 없으면 기본 손절/익절 전략 사용
            if not sell_strategies:
                if is_first_sell_check:
                    logging.debug(f"⚠️ [{code}] 매도 전략 없음 - 기본 손익 전략 사용")
                # 기본 전략: -3% 손절, +5% 익절
                sell_strategies = [{
                    'name': '기본 손익 전략',
                    'conditions': [
                        {'type': 'stop_loss', 'value': -3.0},   # -3% 손절
                        {'type': 'take_profit', 'value': 5.0}   # +5% 익절
                    ]
                }]
            else:
                if is_first_sell_check:
                    logging.debug(f"✅ [{code}] 매도 전략 {len(sell_strategies)}개 로드됨: {strategy_name}")
            
            # 현재 수익률 계산 (전략 평가 전에)
            current_price = market_data.get('current_price', 0)
            profit_rate = (current_price - buy_price) / buy_price * 100 if buy_price > 0 else 0
            
            # 손절 조건 도달 시 디버그 로그 (자주 출력되지 않도록 조건부)
            if profit_rate < -0.6:  # 손절 기준 근처일 때만 디버그
                logging.debug(f"🔍 [{code}] 손절 조건 도달 확인: 수익률={profit_rate:.2f}%, 매입가={buy_price:,}원, 현재가={current_price:,}원")
                logging.debug(f"🔍 [{code}] 로드된 매도 전략 수: {len(sell_strategies)}개")
                for idx, stg in enumerate(sell_strategies):
                    logging.debug(f"🔍 [{code}] 전략 {idx+1}: {stg.get('name', 'N/A')} - 조건: {stg.get('content', 'N/A')}")
            
            # strategy_utils를 사용하여 매도 전략 평가
            condition_met, matched_strategy = strategy_utils.evaluate_sell_strategies(
                code=code,
                strategies=sell_strategies,
                kiwoom_data=market_data,
                chart_data=chart_data,
                buy_price=buy_price,
                buy_time=buy_time,
                portfolio_info=portfolio
            )
                
            if condition_met and matched_strategy:
                # 매도 조건 충족 시, 실제 주문 가능한 수량을 REST API로 최종 확인
                # 부분 익절 비율 확인
                partial_sell_ratio = matched_strategy.get('partial_sell_ratio')

                order_available_qty = 0
                try:
                    if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                        balance_result = self.parent.login_handler.kiwoom_client.get_acnt_balance()
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
                                        logging.info(f"📡 부분 익절 수량 계산: 보유수량 {total_holding_qty}주 * {partial_sell_ratio} = {order_available_qty}주")
                                    else:
                                        # 전량 매도
                                        order_available_qty = self.parent.data_manager.safe_int(stock.get('rmnd_qty', 0))
                                        logging.info(f"📡 전량 매도 수량 조회 (REST API): {code} 주문가능수량 {order_available_qty}주")
                                    break
                    # REST API 조회 실패 시 웹소켓 데이터로 대체
                    if order_available_qty <= 0:
                         if (hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'websocket_client')):
                            ws_balance_data = self.parent.login_handler.websocket_client.balance_data
                            if ws_balance_data and code in ws_balance_data:
                                order_available_qty = ws_balance_data[code].get('order_available_qty', 0)
                                if partial_sell_ratio and 0 < partial_sell_ratio < 1:
                                    total_holding_qty = ws_balance_data[code].get('quantity', 0)
                                    order_available_qty = int(total_holding_qty * partial_sell_ratio)
                                    logging.info(f"💰 부분 익절 수량 계산 (웹소켓 Fallback): 보유수량 {total_holding_qty}주 * {partial_sell_ratio} = {order_available_qty}주")
                                else:
                                    logging.info(f"💰 전량 매도 수량 조회 (웹소켓 Fallback): {code} 주문가능수량 {order_available_qty}주")

                except Exception as qty_check_ex:
                    logging.error(f"❌ [{code}] 주문가능수량 확인 중 오류: {qty_check_ex}")

                if order_available_qty <= 0:
                    logging.warning(f"⚠️ 매도 신호가 발생했으나 주문가능수량이 0주입니다. (다른 주문 처리 중일 수 있음): {code}")
                    return signals

                strategy_display_name = matched_strategy.get('name', strategy_name)
                logging.info(f"📉 매도 신호 발생: {code} - {strategy_display_name}")
                logging.info(f"💰 매입가={buy_price:,}원, 현재가={current_price:,}원, 수익률={profit_rate:.2f}%")
                logging.info(f"📊 보유수량={quantity:,}주, 주문가능수량={order_available_qty:,}주, 매도수량={order_available_qty:,}주")
                
                signals.append({
                    'strategy': matched_strategy.get('name', strategy_name),
                    'code': code,
                    'quantity': order_available_qty,  # 주문가능수량만큼만 매도
                    'price': 0,  # 시장가
                    'reason': f"기술적 지표 기반 매도 조건 충족: {matched_strategy.get('name', '')} (수익률: {profit_rate:.2f}%)"
                })
            else:
                # 매도 조건 미충족 시 현재 수익률 표시 (최초 1회만)
                if is_first_sell_check:
                    logging.debug(f"ℹ️ [{code}] 매도 조건 미충족 (보유 중, 수익률: {profit_rate:.2f}%)")
            
            return signals
            
        except Exception as ex:
            logging.error(f"매도 신호 생성 실패 ({code}): {ex}")
            traceback.print_exc()
            return []
    
    def execute_buy_signals(self, code, signals):
        """매수 신호 실행"""
        try:
            for signal in signals:
                success = self.trader.place_buy_order(
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
            logging.error(f"매수 신호 실행 실패 ({code}): {ex}")
    
    def execute_sell_signals(self, code, signals):
        """매도 신호 실행"""
        try:
            logging.info(f"🔄 [{code}] 매도 신호 실행 시작: {len(signals)}개 신호")
            for idx, signal in enumerate(signals):
                requested_quantity = signal['quantity']
                logging.info(f"   신호 {idx+1}/{len(signals)}: {signal['strategy']}, 수량: {requested_quantity}주, 가격: {signal.get('price', 0)}원 (시장가)")
                
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
                                logging.warning(f"⚠️ [{code}] 주문가능수량 변경 감지: 요청={requested_quantity}주, 실제={actual_order_available_qty}주 (다른 주문으로 인한 변경)")
                except Exception as ws_check_ex:
                    logging.debug(f"⚠️ [{code}] 주문 전 주문가능수량 체크 중 오류: {ws_check_ex}")
                
                # 주문가능수량이 0주 이하면 주문 스킵
                if actual_order_available_qty <= 0:
                    logging.warning(f"⚠️ [{code}] 주문가능수량 0주 - 주문 스킵 (다른 주문 처리 중)")
                    continue
                
                # 실제 주문 가능한 수량으로 제한
                final_quantity = min(requested_quantity, actual_order_available_qty)
                if final_quantity < requested_quantity:
                    logging.info(f"📊 [{code}] 주문 수량 조정: {requested_quantity}주 → {final_quantity}주")
                
                success = self.trader.place_sell_order(
                    code, 
                    final_quantity,  # 조정된 수량 사용
                    signal['price'], 
                    signal['strategy']
                )
                
                if success:
                    logging.info(f"✅ [{code}] 매도 주문 성공: {signal['strategy']} - {final_quantity}주")
                    
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
                    logging.error(f"❌ [{code}] 매도 주문 실패: {signal['strategy']} - {final_quantity}주")
                    
        except Exception as ex:
            logging.error(f"매도 신호 실행 실패 ({code}): {ex}")

# ==================== 자동매매 클래스 ====================
class AutoTrader(QObject):
    """자동매매 관리 클래스"""
    
    def __init__(self, trader, parent):
        try:
            super().__init__()           
            self.trader = trader            
            self.parent = parent            
            self.is_running = True  # 자동매매 항상 활성화
            self.auto_liquidation_executed = False  # 15:15 자동 청산 실행 여부
            logging.debug("🔍 자동매매 실행 상태 초기화 완료 (항상 활성화)")
            
            # evaluation_interval 설정값으로 매매 판단 타이머 초기화
            self.trading_check_timer = QTimer()
            self.trading_check_timer.timeout.connect(self._periodic_trading_check)
            
            # 1분마다 거래 시간 감시 타이머 (거래 시간 외에 사용)
            self.time_monitor_timer = QTimer()
            self.time_monitor_timer.timeout.connect(self._check_trading_time)
            
            logging.debug(f"🔍 자동매매 초기화 완료 ({self.trader.evaluation_interval}초 주기 매매 판단)")
            logging.debug("자동매매 클래스 초기화 완료")
        except Exception as ex:
            logging.error(f"❌ AutoTrader 초기화 실패: {ex}")
            logging.error(f"AutoTrader 초기화 예외 상세: {traceback.format_exc()}")
            raise ex
    
    
    def start_auto_trading(self):
        """자동매매 시작 (거래 시간: evaluation_interval, 거래 시간 외: 1분 타이머)"""
        try:
            if not self.is_running:
                self.is_running = True
                logging.debug("✅ 자동매매 활성화")
            
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
                    logging.info(f"✅ 자동매매 타이머 시작 ({self.trader.evaluation_interval}초 주기 - 거래 시간 내)")
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
                
        except Exception as ex:
            logging.error(f"❌ 자동매매 시작 실패: {ex}")
    
    def stop_auto_trading(self):
        """자동매매 중지 (모든 타이머 정지)"""
        try:
            if self.is_running:
                self.is_running = False
                self.trading_check_timer.stop()
                self.time_monitor_timer.stop()
                logging.debug("🛑 자동매매 중지 (모든 타이머 정지)")
            else:
                logging.debug("자동매매가 이미 중지되어 있습니다")
                
        except Exception as ex:
            logging.error(f"❌ 자동매매 중지 실패: {ex}")
    
    @asyncSlot()
    async def _periodic_trading_check(self):
        """evaluation_interval 주기로 실행되는 주기적 매매 판단 (비동기)"""
        try:
            if not self.is_running:
                logging.debug("⚠️ 자동매매가 실행 중이 아닙니다")
                return
            
            # 시간 체크
            now = datetime.now()
            current_time_str = now.strftime("%H:%M")
            current_hour = now.hour
            current_minute = now.minute
            
            # 15:15 자동 청산 체크
            # 15:15에 자동 청산 (1회만 실행)
            if current_time_str == "15:15" and not self.auto_liquidation_executed:
                logging.info("🕒 15:15 도달 - 모든 보유 종목 자동 청산 시작")
                asyncio.create_task(self.execute_auto_liquidation_async())
                self.auto_liquidation_executed = True
            
            # 다음날을 위해 플래그 리셋 (15:31 이후)
            if current_time_str == "15:31" and self.auto_liquidation_executed:
                self.auto_liquidation_executed = False
                if hasattr(self, '_trading_stopped_logged'):
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
                    self.trading_check_timer.stop()
                    logging.info(f"⏰ 1초 타이머 중지 (거래 시간 전: {current_time_str})")
                    logging.info(f"⏰ 자동매매 대기 중 (시작 시간: 09:00, 현재: {current_time_str})")
                if not self.time_monitor_timer.isActive():
                    self.time_monitor_timer.start(60000)  # 1분
                    logging.info("✅ 시간 모니터링 타이머 시작 (1분 주기)")
                return
            elif current_time_minutes >= end_time_minutes:
                # 15:30 이후 - 1초 타이머 중지, 1분 모니터링 타이머 시작
                if self.trading_check_timer.isActive():
                    self.trading_check_timer.stop()
                    logging.info(f"⏰ 1초 타이머 중지 (거래 종료)")
                    logging.info(f"⏰ 자동매매 종료됨 (종료 시간: 15:30, 현재: {current_time_str})")
                if not self.time_monitor_timer.isActive():
                    self.time_monitor_timer.start(60000)  # 1분
                    logging.info("✅ 시간 모니터링 타이머 시작 (1분 주기)")
                return
            else:
                # 거래 시간 내 - 모니터링 타이머는 중지
                if self.time_monitor_timer.isActive():
                    self.time_monitor_timer.stop()
            
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
                if monitoring_codes:
                    logging.debug(f"🔍 자동매매 모니터링 중: {len(monitoring_codes)}개 종목 - {monitoring_codes}")
                else:
                    logging.debug("🔍 자동매매 실행 중 - 모니터링 종목 없음")
                self._last_status_log_time = current_time
            
            # 15:15 자동 청산 이후에는 매매 중지
            if self.auto_liquidation_executed:
                # 최초 1회만 로그 출력
                if not hasattr(self, '_trading_stopped_logged'):
                    logging.info("⏹️ 15:15 자동 청산 완료 - 모든 매매 활동 중지")
                    self._trading_stopped_logged = True
                return
            
            # 모니터링 중인 모든 종목에 대해 매매 판단 실행 (동시 처리)
            codes = list(self.parent.chart_cache.cache.keys())
            if codes:
                # 병렬 처리하여 성능 향상
                await asyncio.gather(
                    *[self._analyze_and_execute_trading_async(code) for code in codes],
                    return_exceptions=True
                )
                    
        except Exception as ex:
            logging.error(f"❌ 주기적 매매 판단 중 오류: {ex}")
    
    async def _analyze_and_execute_trading_async(self, code):
        """매매 판단 및 실행 비동기 래퍼"""
        try:
            # 동기 함수를 await 가능하게 래핑
            loop = asyncio.get_running_loop()
            # ThreadPoolExecutor에서 실행하여 UI 블로킹 방지
            await loop.run_in_executor(None, self.analyze_and_execute_trading, code)
        except Exception as ex:
            logging.error(f"❌ 비동기 매매 판단 실패 ({code}): {ex}")
    
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
                    self.time_monitor_timer.stop()
                    logging.info("⏰ 시간 모니터링 타이머 중지 (거래 시간 도달)")
                
                if not self.trading_check_timer.isActive():
                    interval_ms = self.trader.evaluation_interval * 1000  # 초 -> 밀리초
                    self.trading_check_timer.start(interval_ms)
                    logging.info("=" * 70)
                    logging.info(f"🚀 자동매매 시작 ({self.trader.evaluation_interval}초 주기, 거래 시간 도달: {current_time_str})")
                    logging.info("=" * 70)
            else:
                # 거래 시간 외 - 계속 대기
                logging.debug(f"⏰ 거래 시간 대기 중 (현재: {current_time_str})")
            
        except Exception as ex:
            logging.error(f"❌ 거래 시간 체크 중 오류: {ex}")
    
    def analyze_and_execute_trading(self, code):
        """ChartDataCache 데이터로 매매 판단 및 실행 (AutoTrader에서 통합 관리)
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
                logging.debug(f"🔍 [{code}] 매매 판단 시작")
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
                    logging.debug(f"ℹ️ [{code}] 차트 데이터 수집 대기 중 (틱:{has_tic}({tic_len}개), 분:{has_min}({min_len}개))")
                return False

            # KiwoomStrategy.evaluate_strategy를 사용하여 매매 판단
            if not hasattr(self.parent, 'objstg') or not self.parent.objstg:
                if is_first_debug:
                    logging.debug(f"ℹ️ [{code}] 전략 객체 초기화 대기 중")
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
                logging.debug(f"✅ [{code}] 매매 판단 데이터 준비 완료 (현재가:{current_price:,}, 거래량:{volume:,})")
            
            # 전략 평가 실행
            self.parent.objstg.evaluate_strategy(code, market_data)
            return True

        except Exception as ex:
            logging.error(f"❌ [{code}] 매매 판단 및 실행 실패: {ex}")
            logging.error(f"상세 에러: {traceback.format_exc()}")
            return False
    
    def execute_auto_liquidation(self):
        """15:15 자동 청산 - 모든 보유 종목 전량 매도 (대화상자 없음)"""
        try:
            logging.info("=" * 70)
            logging.info("🕒 15:15 자동 청산 시작")
            logging.info("=" * 70)
            
            # 보유 종목 목록 생성 (boughtBox에서)
            if not hasattr(self.parent, 'boughtBox') or self.parent.boughtBox.count() == 0:
                logging.info("ℹ️ 보유 중인 종목이 없습니다 - 자동 청산 종료")
                return
            
            sell_items = []
            for i in range(self.parent.boughtBox.count()):
                item = self.parent.boughtBox.item(i)
                item_text = item.text()
                code = item_text.split(' - ')[0] if ' - ' in item_text else item_text.split(' ')[0]
                sell_items.append(code)
            
            logging.info(f"📋 청산 대상: {len(sell_items)}개 종목")
            for code in sell_items:
                logging.info(f"   - {code}")
            
            # 각 종목에 대해 매도 주문 실행
            success_count = 0
            fail_count = 0
            
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
                            logging.info(f"💰 웹소켓 잔고: {code} {quantity:,}주")
                    
                    # 2차: 웹소켓 데이터가 없거나 수량이 0이면 REST API로 조회
                    if quantity <= 0:
                        try:
                            if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                                balance_result = self.parent.login_handler.kiwoom_client.get_acnt_balance()
                                if balance_result:
                                    holdings = balance_result.get('stk_acnt_evlt_prst', balance_result.get('output1', []))
                                    for stock in holdings:
                                        raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                                        stock_code = self.parent.normalize_stock_code(raw_code)
                                        if stock_code == code:
                                            quantity = self.parent.safe_int(stock.get('rmnd_qty', stock.get('hldg_qty', 0)))
                                            logging.info(f"📡 REST API 잔고: {code} {quantity:,}주")
                                            break
                        except Exception as api_ex:
                            logging.error(f"❌ REST API 잔고 조회 실패 ({code}): {api_ex}")
                    
                    # 수량 확인
                    if quantity <= 0:
                        logging.warning(f"⚠️ {code} 보유 수량 없음 - 건너뜀")
                        fail_count += 1
                        continue
                    
                    # 매도 주문 실행 (시장가)
                    if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                        success = self.parent.login_handler.kiwoom_client.place_sell_order(code, quantity, 0, "market")
                        
                        if success:
                            success_count += 1
                            logging.info(f"✅ 자동 청산 성공: {code} {quantity:,}주 시장가 매도")
                        else:
                            fail_count += 1
                            logging.error(f"❌ 자동 청산 실패: {code}")
                    else:
                        fail_count += 1
                        logging.error(f"❌ 키움 클라이언트가 없습니다: {code}")
                        
                except Exception as item_ex:
                    fail_count += 1
                    logging.error(f"❌ {code} 매도 중 오류: {item_ex}")
                    logging.error(f"상세 오류: {traceback.format_exc()}")
            
            # 결과 로그
            logging.info("=" * 70)
            logging.info(f"🕒 15:15 자동 청산 완료")
            logging.info(f"   ✅ 성공: {success_count}개 종목")
            logging.info(f"   ❌ 실패: {fail_count}개 종목")
            logging.info("=" * 70)
            
        except Exception as ex:
            logging.error(f"❌ 자동 청산 실행 중 오류: {ex}")
            logging.error(f"상세 오류: {traceback.format_exc()}")
    
    async def execute_auto_liquidation_async(self):
        """15:15 자동 청산 - 비동기 버전 (종목 병렬 처리)"""
        try:
            logging.info("=" * 70)
            logging.info("🕒 15:15 자동 청산 시작 (비동기)")
            logging.info("=" * 70)
            
            # 보유 종목 목록 생성 (boughtBox에서)
            if not hasattr(self.parent, 'boughtBox') or self.parent.boughtBox.count() == 0:
                logging.info("ℹ️ 보유 중인 종목이 없습니다 - 자동 청산 종료")
                return
            
            sell_items = []
            for i in range(self.parent.boughtBox.count()):
                item = self.parent.boughtBox.item(i)
                item_text = item.text()
                code = item_text.split(' - ')[0] if ' - ' in item_text else item_text.split(' ')[0]
                sell_items.append(code)
            
            logging.info(f"📋 청산 대상: {len(sell_items)}개 종목")
            for code in sell_items:
                logging.info(f"   - {code}")
            
            # 모든 종목 병렬 처리
            if sell_items:
                results = await asyncio.gather(
                    *[self._liquidation_sell_single_stock(code) for code in sell_items],
                    return_exceptions=True
                )
                
                # 결과 집계
                success_count = sum(1 for r in results if r is True)
                fail_count = len(results) - success_count
                
                # 결과 로그
                logging.info("=" * 70)
                logging.info(f"🕒 15:15 자동 청산 완료")
                logging.info(f"   ✅ 성공: {success_count}개 종목")
                logging.info(f"   ❌ 실패: {fail_count}개 종목")
                logging.info("=" * 70)
            else:
                logging.info("ℹ️ 청산할 종목이 없습니다")
            
        except Exception as ex:
            logging.error(f"❌ 자동 청산 실행 중 오류: {ex}")
            logging.error(f"상세 오류: {traceback.format_exc()}")
    
    async def _liquidation_sell_single_stock(self, code):
        """단일 종목 청산 (비동기)"""
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
                    logging.info(f"💰 웹소켓 잔고: {code} {quantity:,}주")
            
            # 2차: 웹소켓 데이터가 없거나 수량이 0이면 REST API로 조회
            if quantity <= 0:
                try:
                    if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                        # ThreadPoolExecutor로 동기 API 호출을 비동기화
                        loop = asyncio.get_running_loop()
                        balance_result = await loop.run_in_executor(
                            None,
                            self.parent.login_handler.kiwoom_client.get_acnt_balance
                        )
                        
                        if balance_result:
                            holdings = balance_result.get('stk_acnt_evlt_prst', balance_result.get('output1', []))
                            for stock in holdings:
                                raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                                stock_code = self.parent.normalize_stock_code(raw_code)
                                if stock_code == code:
                                    quantity = self.parent.safe_int(stock.get('rmnd_qty', stock.get('hldg_qty', 0)))
                                    logging.info(f"📡 REST API 잔고: {code} {quantity:,}주")
                                    break
                except Exception as api_ex:
                    logging.error(f"❌ REST API 잔고 조회 실패 ({code}): {api_ex}")
            
            # 수량 확인
            if quantity <= 0:
                logging.warning(f"⚠️ {code} 보유 수량 없음 - 건너뜀")
                return False
            
            # 매도 주문 실행 (시장가) - ThreadPoolExecutor로 비동기화
            if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                loop = asyncio.get_running_loop()
                success = await loop.run_in_executor(
                    None,
                    lambda: self.parent.login_handler.kiwoom_client.place_sell_order(code, quantity, 0, "market")
                )
                
                if success:
                    logging.info(f"✅ 자동 청산 성공: {code} {quantity:,}주 시장가 매도")
                    return True
                else:
                    logging.error(f"❌ 자동 청산 실패: {code}")
                    return False
            else:
                logging.error(f"❌ 키움 클라이언트가 없습니다: {code}")
                return False
                
        except Exception as item_ex:
            logging.error(f"❌ {code} 매도 중 오류: {item_ex}")
            logging.error(f"상세 오류: {traceback.format_exc()}")
            return False
    
    def _check_risk_management(self, signal_type, signal_data):
        """리스크 관리 확인"""
        try:
            # 매수 시: 기본적인 데이터 유효성만 확인 (실제 현금 확인은 _execute_buy_order에서 수행)
            if signal_type == 'buy':
                required_amount = signal_data.get('amount', 0)
                if required_amount <= 0:
                    logging.warning(f"매수 금액이 유효하지 않음: {required_amount}")
                    return False
                logging.debug(f"매수 신호 확인: 필요 금액 {required_amount}")
            
            # 매도 시: 웹소켓 실시간 잔고 데이터로 보유 종목 확인
            elif signal_type == 'sell':
                balance_data = self.trader.get_balance_data()
                if not balance_data:
                    logging.warning("웹소켓 잔고 데이터가 없습니다")
                    return False
                
                code = signal_data.get('code')
                holdings = balance_data.get('holdings', {})
                if code not in holdings or holdings[code].get('quantity', 0) <= 0:
                    logging.warning(f"보유 종목 없음: {code}")
                    return False
            
            # 손절/익절 확인
            if not self._check_stop_loss_take_profit(signal_type, signal_data):
                return False
            
            logging.debug(f"리스크 관리 확인 통과: {signal_type}")
            return True
            
        except Exception as ex:
            logging.error(f"리스크 관리 확인 실패: {ex}")
            return False
    
    def _check_stop_loss_take_profit(self, signal_type, signal_data):
        """손절/익절 확인"""
        try:
            # 손절/익절 로직 구현
            # 현재는 기본적으로 통과
            return True
        except Exception as ex:
            logging.error(f"손절/익절 확인 실패: {ex}")
            return False
    
    def _execute_buy_order(self, signal_data):
        """매수 주문 실행 (웹소켓 실시간 잔고 데이터 기반)"""
        try:
            code = signal_data.get('code')
            price = signal_data.get('price', 0)
            
            # 보유 종목 확인 (이미 보유 중인 종목은 매수 제외)
            if self.parent and hasattr(self.parent, 'boughtBox'):
                for i in range(self.parent.boughtBox.count()):
                    item_code = self.parent.boughtBox.item(i).text()
                    if item_code == code:
                        logging.info(f"⚠️ 매수 주문 취소: {code}는 이미 보유 중인 종목입니다.")
                        return False
            
            # 실제 투자가능금액 조회 (예수금상세현황 API)
            available_cash = self.trader.get_available_cash()
            logging.debug(f"💰 투자가능금액 조회: {available_cash:,}원")
            
            # 매수가능 종목수 계산 (최대투자종목수 - 현재보유종목수)
            if not hasattr(self.parent, 'login_handler') or not self.parent.login_handler:
                logging.error("login_handler에 접근할 수 없습니다")
                return False
                
            available_buy_count = self.parent.login_handler.get_available_buy_count()
            
            # 매수가능 종목수가 0이면 매수 불가
            if available_buy_count <= 0:
                logging.warning(f"매수 주문 취소: 매수가능 종목수 없음 (최대투자종목수 도달)")
                return False
            
            # 한 종목당 주문금액 계산 (투자가능금액 ÷ 매수가능 종목수)
            order_amount_per_stock = available_cash // available_buy_count
            
            # 현재 가격으로 구매 가능한 수량 계산
            if price > 0:
                # 지정가 주문인 경우
                quantity = order_amount_per_stock // price
                # 최소 1주는 구매하도록 보장
                quantity = max(1, quantity)
            else:
                # 시장가 주문인 경우 현재가를 조회하여 수량 계산
                try:
                    current_price_data = self.trader.client.get_stock_current_price(code)
                    current_price = current_price_data.get('current_price', 0)
                    
                    if current_price > 0:
                        quantity = order_amount_per_stock // current_price
                        # 최소 1주는 구매하도록 보장
                        quantity = max(1, quantity)
                        # 시장가이므로 실제 체결가를 현재가로 설정
                        price = current_price
                    else:
                        # 현재가 조회 실패 시 기본 수량 사용
                        quantity = 1
                        logging.warning(f"현재가 조회 실패, 기본 수량 사용: {code}")
                except Exception as price_ex:
                    # 현재가 조회 중 오류 시 기본 수량 사용
                    quantity = 1
                    logging.error(f"현재가 조회 중 오류 ({code}): {price_ex}")
            
            required_amount = quantity * price
            
            if available_cash < required_amount:
                logging.warning(f"매수 주문 취소: 현금 부족 (필요: {required_amount}, 보유: {available_cash})")
                return False
            
            logging.info(f"💰 매수 주문 실행: {code}, 수량: {quantity}, 가격: {price}")
            logging.info(f"💰 한 종목당 주문금액: {order_amount_per_stock:,}원 (전체: {available_cash:,}원 ÷ {available_buy_count}매수가능종목)")
            logging.info(f"💰 사용 현금: {required_amount:,}원, 잔여 현금: {available_cash - required_amount:,}원")
            
            # 실제 매수 주문 실행 (키움 REST API)
            strategy_name = signal_data.get('strategy', '')
            result = self.trader.place_buy_order(code, quantity, price, strategy_name)
            
            if result:
                logging.info(f"✅ 매수 주문 성공: {code} {quantity}주 @ {price}원")
            else:
                logging.error(f"❌ 매수 주문 실패: {code}")
            
            return result
        except Exception as ex:
            logging.error(f"매수 주문 실행 실패: {ex}")
            return False
    
    def _execute_sell_order(self, signal_data):
        """매도 주문 실행 (웹소켓/REST API 이중 잔고 확인)"""
        try:
            code = signal_data.get('code')
            quantity = signal_data.get('quantity', 1)
            price = signal_data.get('price', 0)
            
            available_quantity = 0
            
            # 1차: 웹소켓 실시간 잔고 데이터 확인 시도
            try:
                balance_data = self.trader.get_balance_data()
                holdings = balance_data.get('holdings', {})
                
                if code in holdings:
                    available_quantity = holdings[code].get('quantity', 0)
                    logging.info(f"💰 웹소켓 잔고 데이터에서 조회: {code} {available_quantity}주")
                else:
                    logging.warning(f"⚠️ 웹소켓 잔고 데이터에 종목이 없습니다: {code}")
            except Exception as ws_ex:
                logging.warning(f"⚠️ 웹소켓 잔고 데이터 조회 실패: {ws_ex}")
            
            # 2차: 웹소켓 데이터가 없거나 수량이 0이면 REST API로 조회
            if available_quantity <= 0:
                logging.info(f"📡 REST API로 보유수량 조회 시도: {code}")
                try:
                    if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                        balance_result = self.parent.login_handler.kiwoom_client.get_acnt_balance()
                        if balance_result:
                            holdings = balance_result.get('stk_acnt_evlt_prst', balance_result.get('output1', []))
                            for stock in holdings:
                                raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                                stock_code = self.parent.normalize_stock_code(raw_code) if hasattr(self.parent, 'normalize_stock_code') else raw_code
                                if stock_code == code:
                                    available_quantity = int(stock.get('rmnd_qty', stock.get('hldg_qty', 0)))
                                    logging.info(f"✅ REST API로 보유수량 조회 성공: {code} {available_quantity}주")
                                    break
                        else:
                            logging.warning("⚠️ REST API 잔고 조회 실패")
                except Exception as api_ex:
                    logging.error(f"❌ REST API 잔고 조회 실패: {api_ex}")
            
            # 최종 수량 확인
            if available_quantity <= 0:
                logging.warning(f"매도 주문 취소: 보유 종목 없음 ({code})")
                return False
            
            if available_quantity < quantity:
                logging.warning(f"매도 주문 취소: 보유 수량 부족 (필요: {quantity}, 보유: {available_quantity})")
                return False
            
            logging.info(f"💰 자동 매도 주문 실행: {code}, 수량: {quantity}, 가격: {price}")
            logging.info(f"💰 보유 수량: {available_quantity}, 매도 후 잔여: {available_quantity - quantity}")
            
            # 실제 매도 주문 실행 (키움 REST API)
            strategy_name = signal_data.get('strategy', '')
            result = self.trader.place_sell_order(code, quantity, price, strategy_name)
            
            if result:
                logging.info(f"✅ 매도 주문 성공: {code} {quantity}주 @ {price}원")
            else:
                logging.error(f"❌ 매도 주문 실패: {code}")
            
            return result
        except Exception as ex:
            logging.error(f"매도 주문 실행 실패: {ex}")
            return False

# ==================== 로그인 핸들러 ====================
class LoginHandler(QObject):
    """로그인 및 연결 관리 클래스"""
    
    # 시그널 정의: 연결 상태가 변경될 때 UI 업데이트를 위해 사용
    connection_status_changed = pyqtSignal(bool)
    
    def __init__(self, parent_window):
        super().__init__()
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
            logging.error(f"target_buy_count 읽기 실패: {ex}")
            return 3  # 기본값
    
    def get_current_holdings_count(self):
        """현재 보유종목 수 조회"""
        try:
            # 1차: 보유종목 리스트박스(boughtBox)에서 직접 확인 (가장 정확함)
            if hasattr(self.parent, 'boughtBox') and self.parent.boughtBox:
                count = self.parent.boughtBox.count()
                logging.debug(f"📊 보유종목 수 (boughtBox): {count}개")
                return count
            
            # 2차: KiwoomTrader의 holdings 확인
            if hasattr(self.parent, 'trader') and self.parent.trader:
                if hasattr(self.parent.trader, 'holdings'):
                    count = len(self.parent.trader.holdings)
                    logging.debug(f"📊 보유종목 수 (trader.holdings): {count}개")
                    return count
            
            # 3차: 웹소켓 실시간 잔고 데이터 확인 (변동이 있을 때만 업데이트됨)
            balance_data = self.parent.trader.get_balance_data()
            holdings = balance_data.get('holdings', {})
            # 수량이 0보다 큰 종목만 카운트
            active_holdings = {code: info for code, info in holdings.items() 
                             if info.get('quantity', 0) > 0}
            count = len(active_holdings)
            logging.debug(f"📊 보유종목 수 (balance_data): {count}개")
            return count
            
        except Exception as ex:
            logging.error(f"보유종목 수 조회 실패: {ex}")
            return 0
    
    def get_available_buy_count(self):
        """매수가능 종목수 계산 (최대투자종목수 - 현재보유종목수)"""
        try:
            max_count = self.parent.trading_manager.get_target_buy_count()
            current_count = self.get_current_holdings_count()
            available_count = max(0, max_count - current_count)
            
            logging.debug(f"📊 투자 종목 현황: 최대 {max_count}종목, 현재 보유 {current_count}종목, 매수가능 {available_count}종목")
            return available_count
        except Exception as ex:
            logging.error(f"매수가능 종목수 계산 실패: {ex}")
            return 1  # 기본값
    
    def load_settings_sync(self):
        """설정 로드 (동기 I/O)"""
        try:
            self.config.read('settings.ini', encoding='utf-8')
            if self.config.has_option('KIWOOM_API', 'simulation'):
                is_simulation = self.config.getboolean('KIWOOM_API', 'simulation')
                self.parent.tradingModeCombo.setCurrentIndex(0 if is_simulation else 1)
            if self.config.has_option('LOGIN', 'autoconnect'):
                self.parent.autoConnectCheckBox.setChecked(self.config.getboolean('LOGIN', 'autoconnect'))
        except Exception as ex:
            logging.error(f"설정 로드 폴백 실패: {ex}")
    
    async def save_settings(self):
        """설정 저장 (비동기 I/O)"""
        try:
            
            # 거래 모드 설정 저장
            is_simulation = (self.parent.tradingModeCombo.currentIndex() == 0)
            self.config.set('KIWOOM_API', 'simulation', str(is_simulation))
            
            # 자동 연결 설정 저장
            self.config.set('LOGIN', 'autoconnect', str(self.parent.autoConnectCheckBox.isChecked()))
            
            # 비동기 파일 쓰기
            config_string = self._config_to_string()
            async with aiofiles.open('settings.ini', 'w', encoding='utf-8') as f:
                await f.write(config_string)
                
        except Exception as ex:
            logging.error(f"설정 저장 실패: {ex}")
            # 동기 방식으로 폴백
            self.save_settings_sync()
    
    def save_settings_sync(self):
        """설정 저장 (동기 I/O)"""
        try:
            # 거래 모드 설정 저장
            is_simulation = (self.parent.tradingModeCombo.currentIndex() == 0)
            self.config.set('KIWOOM_API', 'simulation', str(is_simulation))
            
            # 자동 연결 설정 저장
            self.config.set('LOGIN', 'autoconnect', str(self.parent.autoConnectCheckBox.isChecked()))
            
            # 동기 파일 쓰기
            with open('settings.ini', 'w', encoding='utf-8') as configfile:
                self.config.write(configfile)
        except Exception as ex:
            logging.error(f"설정 저장 폴백 실패: {ex}")
    
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
                logging.error("❌ 키움 클라이언트가 초기화되지 않았습니다. 먼저 API 연결을 시도해주세요.")
                return
            
            # 이미 연결된 웹소켓 클라이언트가 있으면 재사용 (balance_data 보존)
            if hasattr(self, 'websocket_client') and self.websocket_client and self.websocket_client.connected:
                logging.info("✅ 웹소켓 클라이언트가 이미 연결되어 있습니다 (재사용)")
                return
            
            # 기존 balance_data 백업 (웹소켓 재생성 시 데이터 보존)
            existing_balance_data = {}
            if hasattr(self, 'websocket_client') and self.websocket_client and hasattr(self.websocket_client, 'balance_data'):
                existing_balance_data = dict(self.websocket_client.balance_data)
                if existing_balance_data:
                    logging.info(f"💾 기존 웹소켓 balance_data 백업: {list(existing_balance_data.keys())} ({len(existing_balance_data)}개 종목)")
            
            # 웹소켓 클라이언트 초기화
            token = self.kiwoom_client.access_token
            is_mock = self.kiwoom_client.is_mock
            logger = logging.getLogger('KiwoomWebSocketClient')
            
            # 웹소켓 클라이언트 초기화 로그 제거
            self.websocket_client = KiwoomWebSocketClient(token, logger, is_mock, self.parent)
            
            # 백업한 balance_data 복원
            if existing_balance_data:
                self.websocket_client.balance_data = existing_balance_data
                logging.info(f"✅ 웹소켓 balance_data 복원 완료: {list(self.websocket_client.balance_data.keys())} ({len(self.websocket_client.balance_data)}개 종목)")
            
            # 웹소켓 서버에 먼저 연결한 후 실행 (메인 스레드에서 qasync 사용)
            # 연결 시도 로그 제거
            
            # 메인 스레드에서 qasync로 웹소켓 실행
            
            # 웹소켓 클라이언트를 비동기 태스크로 실행
            self.websocket_task = asyncio.create_task(self.websocket_client.run())
            
            # 클라이언트 시작 로그 제거
            
        except Exception as e:
            logging.error(f"❌ 웹소켓 클라이언트 시작 실패: {e}")
            logging.error(f"웹소켓 클라이언트 시작 에러 상세: {traceback.format_exc()}")

    def init_kiwoom_client(self):
        """키움 REST API 클라이언트 초기화"""
        try:
            client = KiwoomRestClient('settings.ini')
            if client.connect():
                # REST API 클라이언트 초기화 로그 제거
                return client
            else:
                logging.error("키움 REST API 클라이언트 초기화 실패")
                return None
        except Exception as ex:
            logging.error(f"키움 REST API 클라이언트 초기화 중 오류: {ex}")
            return None
    
    def handle_api_connection(self):
        """키움 REST API 연결 처리"""
        try:
            # 설정 저장 (동기 방식으로 안전하게 실행)
            try:
                self.save_settings_sync()
            except Exception as ex:
                logging.error(f"설정 저장 실패: {ex}")
            
            # 키움 REST API 연결
            self.kiwoom_client = self.init_kiwoom_client()
            
            if self.kiwoom_client and self.kiwoom_client.is_connected:
                # 연결 상태 업데이트
                self.parent.update_connection_ui(is_connected=True)
                
                # 거래 모드에 따른 메시지
                mode = "모의투자" if self.parent.tradingModeCombo.currentIndex() == 0 else "실제투자"
                logging.debug(f"키움 REST API 연결 성공! 거래 모드: {mode}")
                
                # 트레이더 객체 생성 (API 연결 성공 후 즉시)
                try:
                    if not hasattr(self.parent, 'trader') or not self.parent.trader:
                        buycount = int(self.parent.buycountEdit.text())
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
                    logging.error(f"❌ 트레이더 객체 생성 실패: {trader_ex}")
                    logging.error(f"트레이더 생성 예외 상세: {traceback.format_exc()}")
                
            else:
                logging.error("키움 REST API 연결 실패! settings.ini 파일의 appkey와 appsecret을 확인해주세요.")
                self.parent.update_connection_ui(is_connected=False)
                
        except Exception as ex:
            logging.error(f"API 연결 처리 실패: {ex}")
    
    async def _handle_connection_toggle_async(self):
        """연결/해제 버튼 클릭 비동기 처리"""
        try:
            # 키움 클라이언트가 있고 연결된 상태인지 확인
            is_connected = (hasattr(self, 'kiwoom_client') and 
                            self.kiwoom_client and 
                            self.kiwoom_client.is_connected)

            if is_connected:
                # --- 연결 해제 로직 ---
                logging.info("🔌 API 연결 해제를 시도합니다...")
                # 웹소켓 종료
                if hasattr(self, 'websocket_client') and self.websocket_client:
                    await self.websocket_client.disconnect()
                # REST 클라이언트 연결 해제
                self.kiwoom_client.disconnect()
                
                # UI 업데이트 시그널 발생
                self.connection_status_changed.emit(False)
                logging.info("✅ API 연결이 해제되었습니다.")

            else:
                # --- 연결 로직 ---
                logging.info("🔌 API 연결을 시도합니다...")
                self.handle_api_connection()
                await self.start_websocket_client()

                # 연결 성공 시 UI 업데이트는 post_login_setup에서 처리됨
                # 여기서는 연결 시도 상태를 UI에 반영할 수 있음 (예: 버튼 텍스트 변경)
                self.connection_status_changed.emit(True) # 임시로 연결됨 상태로 변경
        except Exception as ex:
            logging.error(f"❌ 연결/해제 처리 중 오류: {ex}")

# ==================== MyWindow Manager 클래스들 ====================

class DataManager:
    """데이터 조회 및 관리 매니저"""
    
    def __init__(self, parent):
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
            logging.error(f"종목코드 정규화 실패 ({code}): {ex}")
            return str(code) if code else ""
    
    def normalize_stock_input(self, stock_input):
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
                stock_code = self.get_stock_code_by_name(stock_input)
                if stock_code:
                    return stock_code, stock_input
                else:
                    return None, None
                    
        except Exception as ex:
            logging.error(f"종목 입력 정규화 실패: {ex}")
            return None, None
    
    def get_stock_name_by_code(self, stock_code):
        """종목코드로 종목명 조회 - API 호출 제거됨"""
        # API 제한 초과 방지를 위해 종목코드만 반환
        return f"종목{stock_code}"
    
    def get_stock_code_by_name(self, stock_name):
        """종목명으로 종목코드 조회 - 키움 REST API fn_ka10099 사용"""
        try:
            # 모의투자 여부에 따라 서버 선택
            if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'kiwoom_client'):
                is_mock = self.parent.login_handler.kiwoom_client.is_mock
                kiwoom_client = self.parent.login_handler.kiwoom_client
                
                # fn_ka10099 API 호출 (종목명 검색)
                params = {
                    "stk_nm": stock_name  # 종목명
                }
                
                response = kiwoom_client.call_api("/uapi/domestic-stock/v1/quotations/search-info", params)
                
                if response and response.get('output'):
                    output = response['output']
                    if isinstance(output, list) and len(output) > 0:
                        # 첫 번째 검색 결과의 종목코드 반환
                        first_result = output[0]
                        stock_code = first_result.get('pdno', '')  # pdno: 종목코드
                        if stock_code:
                            # A 접두사 제거
                            if stock_code.startswith('A'):
                                stock_code = stock_code[1:]
                            return stock_code
                
            return None
            
        except Exception as ex:
            logging.error(f"종목명 검색 실패 ({stock_name}): {ex}")
            return None


class MonitoringManager:
    """모니터링 종목 관리 매니저"""
    
    def __init__(self, parent: 'MyWindow'):
        self.parent = parent
    
    def add_stock_to_monitoring(self, code, name):
        """모니터링 리스트박스에 종목 추가"""
        try:
            # 중복 체크
            for i in range(self.parent.monitoringBox.count()):
                item_text = self.parent.monitoringBox.item(i).text()
                if code in item_text:
                    logging.debug(f"종목이 이미 모니터링 목록에 있습니다: {code}")
                    return True
            
            # 리스트박스에 추가
            item_text = f"{code}"  # 종목코드만 표시
            self.parent.monitoringBox.addItem(item_text)
            logging.debug(f"✅ 모니터링 종목 추가: {item_text}")
            
            # 차트 캐시에 추가
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                self.parent.chart_cache.add_monitoring_stock(code)
            
            # 실시간 체결 데이터 구독
            if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'websocket_client'):
                ws_client = self.parent.login_handler.websocket_client
                if ws_client and ws_client.connected:
                    # qasync 환경에서 안전하게 태스크 생성
                    try:
                        asyncio.get_running_loop()  # 루프 확인
                        asyncio.create_task(ws_client.subscribe_stock_execution_data([code], 'monitoring'))
                        logging.debug(f"📡 실시간 체결 데이터 구독: {code}")
                    except RuntimeError:
                        # 이벤트 루프가 없으면 무시 (정상적인 상황일 수 있음)
                        pass
            
            return True
            
        except Exception as ex:
            logging.error(f"모니터링 종목 추가 실패 ({code}): {ex}")
            logging.error(f"모니터링 추가 예외 상세: {traceback.format_exc()}")
            return False
    
    async def add_stock_to_monitoring_async(self, code, name):
        """모니터링 리스트박스에 종목 추가 (비동기 버전)"""
        try:
            # 중복 체크
            for i in range(self.parent.monitoringBox.count()):
                item_text = self.parent.monitoringBox.item(i).text()
                if code in item_text:
                    logging.debug(f"종목이 이미 모니터링 목록에 있습니다: {code}")
                    return True
            
            # 리스트박스에 추가
            item_text = f"{code}"  # 종목코드만 표시
            self.parent.monitoringBox.addItem(item_text)
            logging.debug(f"✅ 모니터링 종목 추가: {item_text}")
            
            # 차트 캐시에 추가
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                self.parent.chart_cache.add_monitoring_stock(code)
            
            # 실시간 체결 데이터 구독 (await 사용)
            if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'websocket_client'):
                ws_client = self.parent.login_handler.websocket_client
                if ws_client and ws_client.connected:
                    await ws_client.subscribe_stock_execution_data([code], 'monitoring')
            return True
        except Exception as ex:
            logging.error(f"모니터링 종목 추가 실패 (async) ({code}): {ex}")
            return False

    def remove_stock_from_monitoring(self, code):
        """모니터링 리스트박스에서 종목 제거"""
        try:
            # 리스트박스에서 제거
            for i in range(self.parent.monitoringBox.count()):
                item = self.parent.monitoringBox.item(i)
                if code in item.text():
                    self.parent.monitoringBox.takeItem(i)
                    logging.debug(f"✅ 모니터링 종목 제거: {code}")
                    break
            
            # 차트 캐시에서도 제거
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
                        logging.debug(f"📡 실시간 구독 해제: {code}")
                    except RuntimeError:
                        logging.warning("⚠️ 이벤트 루프가 없어 구독 해제를 건너뜁니다")
            
            return True
            
        except Exception as ex:
            logging.error(f"모니터링 종목 제거 실패 ({code}): {ex}")
            return False
    
    def remove_condition_stocks_from_monitoring(self, seq):
        """조건검색으로 추가된 종목들을 모니터링에서 제거"""
        try:
            # 조건검색 결과에서 종목 목록 가져오기
            if seq not in self.parent.condition_search_results:
                logging.debug(f"조건검색 결과 없음 (seq: {seq})")
                return
            
            stock_codes = self.parent.condition_search_results.get(seq, [])
            logging.info(f"조건검색 종목 제거 시작: {len(stock_codes)}개 (seq: {seq})")
            
            # 각 종목을 모니터링에서 제거
            for code in stock_codes:
                self.remove_stock_from_monitoring(code)
            
            # 조건검색 결과 딕셔너리에서 제거
            del self.parent.condition_search_results[seq]
            logging.info(f"조건검색 종목 제거 완료 (seq: {seq})")
            
        except Exception as ex:
            logging.error(f"조건검색 종목 제거 실패 (seq: {seq}): {ex}")
            logging.error(f"제거 예외 상세: {traceback.format_exc()}")
    
    def extract_monitoring_stock_codes_enhanced(self):
        """모니터링 종목 코드 추출 및 로그 출력 - 강화된 예외 처리"""
        try:
            logging.debug("🔧 모니터링 종목 코드 추출 시작")
            logging.debug(f"현재 스레드: {threading.current_thread().name}")
            logging.debug(f"메인 스레드 여부: {threading.current_thread() is threading.main_thread()}")
            logging.debug("=" * 50)
            logging.debug("📋 모니터링 종목 코드 추출 시작")
            logging.debug("=" * 50)
            
            # 모니터링 종목 코드 추출
            monitoring_codes = self.get_monitoring_stock_codes()
            logging.debug(f"모니터링 종목 코드 추출: {monitoring_codes}")
            logging.debug(f"📋 모니터링 종목: {monitoring_codes}")
            
            logging.debug("=" * 50)
            logging.debug("✅ 모니터링 종목 코드 추출 완료")
            logging.debug("=" * 50)
            
            # 모니터링 종목 코드 추출 완료 후 차트 캐시 업데이트
            logging.debug(f"📋 모니터링 종목 코드 추출 완료: {monitoring_codes}")
            
            # 주식체결 실시간 구독 추가
            try:
                if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'kiwoom_client'):
                    # 웹소켓 클라이언트 참조가 제거되어 주식체결 구독 기능 비활성화
                    # 주식체결 구독은 별도로 관리되어야 함
                    logging.debug(f"주식체결 구독 기능은 별도로 관리됩니다: {monitoring_codes}")
                else:
                    logging.warning("⚠️ 키움 클라이언트가 초기화되지 않았습니다")
            except Exception as exec_sub_ex:
                logging.error(f"❌ 주식체결 구독 실패: {exec_sub_ex}")
                logging.error(f"주식체결 구독 예외 상세: {traceback.format_exc()}")
            
            # 차트 데이터 캐시 업데이트 (중요!)
            try:
                if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                    logging.debug(f"🔧 차트 캐시 업데이트 시작: {monitoring_codes}")
                    self.parent.chart_cache.update_monitoring_stocks(monitoring_codes)
                    logging.debug("✅ 차트 캐시 업데이트 완료")
                else:
                    logging.warning("⚠️ 차트 캐시가 초기화되지 않았습니다")
            except Exception as cache_ex:
                logging.error(f"❌ 차트 캐시 업데이트 실패: {cache_ex}")
                logging.error(f"차트 캐시 업데이트 예외 상세: {traceback.format_exc()}")
            
            return monitoring_codes
                
        except Exception as ex:
            logging.error(f"❌ 모니터링 종목 코드 추출 실패: {ex}")
            logging.error(f"추출 예외 상세: {traceback.format_exc()}")
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
            monitoring_box = self.parent.monitoringBox
            
            for i in range(monitoring_box.count()):
                item = monitoring_box.item(i)
                if not item:
                    continue
                    
                item_text = item.text().strip()
                if not item_text:
                    continue
                
                # 다양한 형식 지원
                if ' - ' in item_text:
                    # "종목코드 - 종목명" 형식
                    code = item_text.split(' - ')[0].strip()
                elif ' ' in item_text:
                    # "종목코드 종목명" 형식 (공백 구분)
                    code = item_text.split()[0].strip()
                else:
                    # "종목코드" 단독
                    code = item_text
                
                # 'A' 접두사 제거
                if code.startswith('A'):
                    code = code[1:]
                
                # 6자리 종목코드만 허용
                if code and code.isdigit() and len(code) == 6:
                    stock_codes.append(code)
            
            logging.debug(f"모니터링 종목 코드 추출: {len(stock_codes)}개 - {stock_codes}")
            return stock_codes
            
        except Exception as ex:
            logging.error(f"모니터링 종목 코드 추출 실패: {ex}")
            return []
    
    def subscribe_realtime_execution_data(self, code):
        """실시간 체결 데이터 구독 시작"""
        try:
            # 웹소켓 클라이언트가 연결되어 있는지 확인
            if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'websocket_client'):
                websocket_client = self.parent.login_handler.websocket_client
                if websocket_client and websocket_client.connected:
                    # qasync 환경에서 안전하게 태스크 생성
                    try:
                        asyncio.get_running_loop()  # 루프 확인
                        asyncio.create_task(websocket_client.subscribe_stock_execution_data([code], 'monitoring'))
                        logging.debug(f"📡 모니터링 종목 실시간 체결(0B) 구독 요청: {code}")
                    except RuntimeError:
                        logging.warning("⚠️ 이벤트 루프가 없어 구독을 건너뜁니다")
                else:
                    logging.warning(f"⚠️ 웹소켓이 연결되지 않아 실시간 구독을 시작할 수 없습니다: {code}")
            else:
                logging.warning(f"⚠️ 웹소켓 클라이언트가 없어 실시간 구독을 시작할 수 없습니다: {code}")
                
        except Exception as ex:
            logging.error(f"❌ 실시간 체결 데이터 구독 실패: {code} - {ex}")
    
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
                        logging.debug(f"📡 실시간 체결 데이터 구독 해제: {code}")
                    except RuntimeError:
                        logging.warning("⚠️ 이벤트 루프가 없어 구독 해제를 건너뜁니다")
                else:
                    logging.warning(f"⚠️ 웹소켓이 연결되지 않아 실시간 구독 해제를 할 수 없습니다: {code}")
            else:
                logging.warning(f"⚠️ 웹소켓 클라이언트가 없어 실시간 구독 해제를 할 수 없습니다: {code}")
                
        except Exception as ex:
            logging.error(f"❌ 실시간 체결 데이터 구독 해제 실패: {code} - {ex}")


class UIComponentsManager:
    """UI 컴포넌트 관리 매니저"""
    
    def __init__(self, parent):
        self.parent = parent
    
    def _safe_create_task(self, coro):
        """qasync 환경에서 안전하게 태스크 생성"""
        try:
            asyncio.get_running_loop()  # 루프 확인
            asyncio.create_task(coro)
        except RuntimeError:
            logging.warning("⚠️ 이벤트 루프가 없어 비동기 작업을 실행할 수 없습니다")
    
    def update_connection_ui(self, is_connected):
        """연결 상태에 따라 UI를 업데이트하는 중앙 함수"""
        try:
            if is_connected:
                self.parent.connectionStatusLabel.setText("연결 상태: 연결됨")
                self.parent.connectionStatusLabel.setProperty("class", "connected")
                self.parent.connectButton.setText("해제")
                self.parent.connectButton.setProperty("class", "danger")
                self.parent.tradingModeCombo.setEnabled(False)
            else:
                self.parent.connectionStatusLabel.setText("연결 상태: 미연결")
                self.parent.connectionStatusLabel.setProperty("class", "disconnected")
                self.parent.connectButton.setText("연결")
                self.parent.connectButton.setProperty("class", "success")
                self.parent.tradingModeCombo.setEnabled(True)

            # 스타일시트 갱신
            self.parent.style().polish(self.parent.connectionStatusLabel)
            self.parent.style().polish(self.parent.connectButton)
            self.parent.style().polish(self.parent.tradingModeCombo)

        except Exception as ex:
            logging.error(f"❌ 연결 UI 업데이트 실패: {ex}")

    def update_condition_status(self, status, count=None):
        """조건검색 상태 표시 업데이트"""
        try:
            if count is not None:
                status_text = f"{status} ({count}개 종목)"
            else:
                status_text = status
            
            # UI 스레드에서 실행
            if hasattr(self.parent, 'conditionStatusLabel'):
                self.parent.conditionStatusLabel.setText(status_text)
            
        except Exception as ex:
            logging.error(f"조건검색 상태 업데이트 실패: {ex}")
    
    def display_deposit_info(self, deposit_data):
        """예수금상세현황 정보 표시 (간소화)"""
        try:
            if deposit_data:
                # 응답 데이터는 직접 루트에 있음
                data = deposit_data
                
                # 주요 정보만 간단히 표시
                entr = self.parent.data_manager.safe_int(data.get('entr', '0'))
                pymn_alow = self.parent.data_manager.safe_int(data.get('pymn_alow_amt', '0'))
                ord_alow = self.parent.data_manager.safe_int(data.get('ord_alow_amt', '0'))
                
                logging.info(f"💰 예수금: {entr:,}원, 출금가능: {pymn_alow:,}원, 주문가능: {ord_alow:,}원")
                
            else:
                logging.warning("예수금상세현황 데이터를 찾을 수 없습니다")
            
        except Exception as ex:
            logging.error(f"예수금상세현황 정보 표시 실패: {ex}")
    
    def update_acnt_balance_display(self, balance_data):
        """잔고 정보 표시 업데이트"""
        try:
            total_assets = balance_data.get('total_assets', 0)
            holdings_count = balance_data.get('holdings_count', 0)
            
            balance_text = f"총 자산: {total_assets:,}원\n"
            balance_text += f"보유 종목: {holdings_count}개"
            
            # balanceLabel이 존재하는 경우에만 업데이트
            if hasattr(self.parent, 'balanceLabel') and self.parent.balanceLabel:
                self.parent.balanceLabel.setText(balance_text)
            else:
                # balanceLabel이 없는 경우 로그로만 출력
                logging.debug(f"잔고 정보: {balance_text}")
            
            # 투자 현황표 업데이트
            self.update_stock_table()
            
        except Exception as ex:
            logging.error(f"잔고 정보 업데이트 실패: {ex}")
    
    def update_stock_table(self):
        """투자 현황표 업데이트 (실시간 잔고 데이터 기반)"""
        try:
            # stock_table이 없으면 리턴
            if not hasattr(self.parent, 'stock_table'):
                logging.debug("⚠️ stock_table이 없습니다")
                return
            
            # 웹소켓 클라이언트에서 실시간 잔고 데이터 가져오기
            if not hasattr(self.parent, 'login_handler') or not self.parent.login_handler:
                logging.debug("⚠️ login_handler 객체가 없습니다")
                return
            
            if not hasattr(self.parent.login_handler, 'websocket_client') or not self.parent.login_handler.websocket_client:
                logging.debug("⚠️ websocket_client가 없습니다")
                return
            
            ws_client = self.parent.login_handler.websocket_client
            if not hasattr(ws_client, 'balance_data'):
                logging.debug("⚠️ ws_client.balance_data가 없습니다")
                return
            
            # balance_data의 복사본을 사용 (동시성 문제 방지)
            balance_data = dict(ws_client.balance_data)
            
            # 디버그 로그: 업데이트할 종목 목록
            logging.debug(f"📊 투자 현황표 업데이트 시작: {list(balance_data.keys())} ({len(balance_data)}개 종목)")
            logging.debug(f"   ws_client.balance_data 원본: {list(ws_client.balance_data.keys())}")
            
            # 테이블 초기화
            self.parent.stock_table.setRowCount(0)
            
            # 보유 종목이 없으면 리턴
            if not balance_data:
                logging.info("📊 투자 현황표: 보유 종목 없음")
                return
            
            # 종목별로 테이블에 추가
            row = 0
            for stock_code, stock_info in balance_data.items():
                try:
                    self.parent.stock_table.insertRow(row)
                    
                    # 종목코드
                    code_item = QTableWidgetItem(stock_code)
                    code_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.parent.stock_table.setItem(row, 0, code_item)
                    
                    # 현재가 (매매 판단에 사용하는 현재가 사용 - chart_cache의 틱 데이터)
                    current_price = stock_info.get('current_price', 0)  # 기본값은 웹소켓 현재가
                    
                    # chart_cache에서 매매 판단시 사용하는 현재가 가져오기
                    if (hasattr(self.parent, 'chart_cache') and self.parent.chart_cache):
                        cache_data = self.parent.chart_cache.get_cached_data(stock_code)
                        if cache_data:
                            tic_data = cache_data.get('tic_data', {})
                            if tic_data and tic_data.get('close'):
                                tic_close_list = tic_data.get('close', [])
                                if len(tic_close_list) > 0:
                                    # 매매 판단에 사용하는 현재가 (틱 데이터의 마지막 종가)
                                    current_price = tic_close_list[-1]
                    
                    price_item = QTableWidgetItem(f"{current_price:,.0f}")
                    price_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    self.parent.stock_table.setItem(row, 1, price_item)
                    
                    # 보유수량
                    quantity = stock_info.get('quantity', 0)
                    qty_item = QTableWidgetItem(f"{quantity:,}")
                    qty_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    self.parent.stock_table.setItem(row, 2, qty_item)
                    
                    # 매입단가 (평균단가)
                    buy_price = stock_info.get('average_price', 0)
                    buy_item = QTableWidgetItem(f"{buy_price:,.0f}")
                    buy_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    self.parent.stock_table.setItem(row, 3, buy_item)
                    
                    # 평가손익 (매매 판단시 사용하는 현재가 기준으로 재계산)
                    evaluation_amount = quantity * current_price
                    purchase_amount = quantity * buy_price
                    profit_loss = evaluation_amount - purchase_amount
                    
                    pl_item = QTableWidgetItem(f"{profit_loss:,.0f}")
                    pl_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    
                    # 손익에 따라 색상 변경
                    if profit_loss > 0:
                        pl_item.setForeground(QColor(0, 128, 0))  # 녹색 (수익)
                    elif profit_loss < 0:
                        pl_item.setForeground(QColor(255, 0, 0))  # 빨강 (손실)
                    
                    self.parent.stock_table.setItem(row, 4, pl_item)
                    
                    # 수익률(%) (매매 판단시 사용하는 현재가 기준으로 재계산)
                    profit_loss_rate = (profit_loss / purchase_amount * 100) if purchase_amount > 0 else 0
                    
                    rate_item = QTableWidgetItem(f"{profit_loss_rate:.2f}")
                    rate_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    
                    # 수익률에 따라 색상 변경
                    if profit_loss_rate > 0:
                        rate_item.setForeground(QColor(0, 128, 0))  # 녹색 (수익)
                    elif profit_loss_rate < 0:
                        rate_item.setForeground(QColor(255, 0, 0))  # 빨강 (손실)
                    
                    self.parent.stock_table.setItem(row, 5, rate_item)
                    
                    logging.debug(f"  📌 {stock_code}: 현재가 {current_price:,}원(매매판단용), 매입단가 {buy_price:,}원, 수량 {quantity:,}주, 손익 {profit_loss:,.0f}원 ({profit_loss_rate:+.2f}%)")
                    
                    row += 1
                    
                except Exception as item_ex:
                    logging.error(f"❌ 투자 현황표 항목 추가 실패 ({stock_code}): {item_ex}")
                    continue
            
            # 업데이트 완료 로그
            logging.debug(f"✅ 투자 현황표 업데이트 완료: {row}개 종목 표시됨")
            
        except Exception as ex:
            logging.error(f"❌ 투자 현황표 업데이트 실패: {ex}")
            logging.error(f"투자 현황표 업데이트 예외: {traceback.format_exc()}")
    
    def update_order_result(self, code, order_type, quantity, price, success):
        """주문 결과 업데이트"""
        try:
            status = "성공" if success else "실패"
            action = "매수" if order_type == "buy" else "매도"
            
            message = f"{action} 주문 {status}: {code} {quantity}주 @ {price}"
            
            if success:
                logging.debug(message)
                
                # 주문 성공 시 (실시간 잔고 데이터가 자동으로 보유 종목을 관리함)
                if order_type == "buy":
                    # 매수 성공: 실시간 잔고 데이터가 자동으로 보유 종목에 추가
                    pass
                elif order_type == "sell":
                    # 매도 성공: 실시간 잔고 데이터가 자동으로 보유 종목에서 제거
                    pass
            else:
                logging.error(message)
                
        except Exception as ex:
            logging.error(f"주문 결과 업데이트 실패: {ex}")
    
    def update_strategy_result(self, code, action, data):
        """전략 결과 업데이트"""
        try:
            strategy = data.get('strategy', '')
            reason = data.get('reason', '')
            
            message = f"전략 실행: {code} {action} - {strategy} ({reason})"
            logging.debug(message)
            
        except Exception as ex:
            logging.error(f"전략 결과 업데이트 실패: {ex}")
    
    def on_chart_data_updated(self, code):
        """차트 데이터 업데이트 시그널 핸들러"""
        try:
            # 실시간 차트 위젯이 현재 선택된 종목과 같으면 업데이트
            if (hasattr(self.parent, 'realtime_chart_widget') and self.parent.realtime_chart_widget and 
                self.parent.realtime_chart_widget.current_code == code):
                
                # 캐시에서 최신 데이터 가져오기
                if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                    cache_data = self.parent.chart_cache.get_cached_data(code)
                    if cache_data:
                        self.parent.realtime_chart_widget.update_chart_data(
                            cache_data.get('tic_data'), 
                            cache_data.get('min_data')
                        )
                        logging.debug(f"📊 실시간 차트 업데이트: {code}")
                        
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 업데이트 처리 실패: {code} - {ex}")
    
    def add_balance_stock_to_holdings(self, balance_info):
        """실시간 잔고 데이터를 받아 보유 종목에 자동 추가 (UI 스레드 안전)"""
        try:
            
            # UI 스레드에서 실행되는지 확인
            if not QThread.isMainThread():
                logging.warning("add_balance_stock_to_holdings가 메인 스레드가 아닌 곳에서 호출됨")
                return
            
            stock_code = balance_info.get('stock_code', '')
            stock_name = balance_info.get('stock_name', '알 수 없음')
            quantity = balance_info.get('quantity', 0)
            
            if not stock_code or quantity <= 0:
                return
            
            # 이미 보유종목 리스트에 있는지 확인
            existing_items = []
            for i in range(self.parent.boughtBox.count()):
                existing_items.append(self.parent.boughtBox.item(i).text())
            
            # "종목코드 - 종목명" 형식으로 표시 (기존 형식과 일치)
            stock_display = f"{stock_code} - {stock_name}"
            
            # 중복되지 않는 경우만 보유종목 리스트에 추가
            if stock_display not in existing_items:
                self.parent.boughtBox.addItem(stock_display)
                logging.debug(f"✅ 실시간 잔고 종목을 보유종목에 자동 추가: {stock_display} ({quantity}주)")
                
                # 실시간 잔고 데이터는 이미 모니터링에 추가되어 있으므로 보유종목 리스트에만 추가
            else:
                logging.debug(f"이미 보유종목에 존재: {stock_display}")
                
        except Exception as ex:
            logging.error(f"실시간 잔고 종목 추가 실패: {ex}")
    
    def init_ui(self):
        """UI 초기화 (탭 구조)"""
        try:
            
            self.parent.setWindowTitle("키움 REST API 자동매매 프로그램 v3.0")
            self.parent.setGeometry(0, 0, 1900, 980)
            
            # 전체 애플리케이션 스타일 적용
            self.apply_modern_style()
            
            # ===== 메인 탭 위젯 생성 =====
            self.parent.tab_widget = QTabWidget()
            
            # 탭 1: 실시간 매매
            self.parent.trading_tab = QWidget()
            self.init_trading_tab()
            self.parent.tab_widget.addTab(self.parent.trading_tab, "실시간 매매")
            
            # 탭 2: 백테스팅
            self.parent.backtest_tab = QWidget()
            self.parent.backtest_manager.init_backtest_tab()
            self.parent.tab_widget.addTab(self.parent.backtest_tab, "백테스팅")
            
            # 메인 레이아웃
            main_layout = QVBoxLayout()
            main_layout.addWidget(self.parent.tab_widget)
            self.parent.setLayout(main_layout)
            
            # 창 표시 안정성을 위한 설정
            self.parent.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint | Qt.WindowType.WindowCloseButtonHint)
            
        except Exception as ex:
            logging.error(f"UI 초기화 실패: {ex}")
        
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
        
        self.parent.setStyleSheet(style)
    
    def init_trading_tab(self):
        """실시간 매매 탭 초기화"""
        
        parent = self.parent
        
        # ===== 키움 REST API 연결 영역 =====
        loginLayout = QVBoxLayout()

        # API 연결 상태 표시
        statusLayout = QHBoxLayout()
        parent.connectionStatusLabel = QLabel("연결 상태: 미연결")
        parent.connectionStatusLabel.setProperty("class", "disconnected")
        statusLayout.addWidget(parent.connectionStatusLabel)

        statusLayout.addStretch()
        # 자동 연결 설정 (연결 상태 옆으로 이동)
        parent.autoConnectCheckBox = QCheckBox("자동 연결")
        statusLayout.addWidget(parent.autoConnectCheckBox)
        
        # 모의투자/실제투자 구분
        tradingModeLayout = QHBoxLayout()
        
        parent.tradingModeCombo = QComboBox()
        parent.tradingModeCombo.addItem("모의투자")
        parent.tradingModeCombo.addItem("실제투자")
        parent.tradingModeCombo.setFixedWidth(120)
        tradingModeLayout.addWidget(parent.tradingModeCombo)
        
        tradingModeLayout.addStretch()
        # 연결/해제 토글 버튼 추가
        parent.connectButton = QPushButton("연결")
        parent.connectButton.setFixedWidth(80)
        parent.connectButton.setProperty("class", "success") # 초기 상태는 '연결' (성공 클래스)
        tradingModeLayout.addWidget(parent.connectButton)

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
        
        parent.buycountEdit = QLineEdit(str(saved_buycount))
        buycountLayout.addWidget(parent.buycountEdit)
        buycountLayout.addStretch()
        parent.buycountButton = QPushButton("설정")
        parent.buycountButton.setFixedWidth(70)
        buycountLayout.addWidget(parent.buycountButton)

        # ===== 모니터링 종목 리스트 =====
        monitoringBoxLayout = QVBoxLayout()
        listBoxLabel = QLabel("모니터링 종목:")
        monitoringBoxLayout.addWidget(listBoxLabel)
        
        # 종목 입력 영역
        inputLayout = QHBoxLayout()
        parent.stockInputEdit = QLineEdit()
        parent.stockInputEdit.setPlaceholderText("종목명 또는 종목코드 입력 (예: 삼성전자, 005930)")
        inputLayout.addWidget(parent.stockInputEdit)
        parent.addStockButton = QPushButton("추가")
        parent.addStockButton.setFixedWidth(60)
        inputLayout.addWidget(parent.addStockButton)
        monitoringBoxLayout.addLayout(inputLayout)
        
        parent.monitoringBox = QListWidget()
        parent.monitoringBox.setEnabled(False)
        monitoringBoxLayout.addWidget(parent.monitoringBox, 1)
        
        # 모니터링 종목은 조건검색으로만 추가됨
        firstButtonLayout = QHBoxLayout()
        parent.buyButton = QPushButton("매입")
        parent.buyButton.setProperty("class", "success")
        firstButtonLayout.addWidget(parent.buyButton)
        parent.deleteFirstButton = QPushButton("삭제")        
        parent.deleteFirstButton.setProperty("class", "danger")
        firstButtonLayout.addWidget(parent.deleteFirstButton)        
        monitoringBoxLayout.addLayout(firstButtonLayout)

        # ===== 보유 종목 리스트 =====
        boughtBoxLayout = QVBoxLayout()
        boughtBoxLabel = QLabel("보유 종목:")
        boughtBoxLayout.addWidget(boughtBoxLabel)
        parent.boughtBox = QListWidget()
        parent.boughtBox.setEnabled(False)
        boughtBoxLayout.addWidget(parent.boughtBox, 1)
        secondButtonLayout = QHBoxLayout()
        parent.sellButton = QPushButton("매도")
        parent.sellButton.setProperty("class", "danger")
        secondButtonLayout.addWidget(parent.sellButton)
        parent.sellAllButton = QPushButton("전부 매도")
        parent.sellAllButton.setProperty("class", "danger")
        secondButtonLayout.addWidget(parent.sellAllButton)     
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
        parent.realtime_chart_widget = PyQtGraphRealtimeWidget(parent)
        
        parent.chart_layout = chartLayout  # 차트 레이아웃 참조 저장
        
        # 실시간 차트 위젯을 레이아웃에 추가
        chartLayout.addWidget(parent.realtime_chart_widget)
               
        # 차트 캐시 업데이트 시 매매 판단 (효율적인 구조)
        if not parent.chart_cache:
            parent.chart_cache = ChartDataCache(None, parent)  # 트레이더는 API 연결 후 설정됨
        
        # 캐시 데이터 업데이트 시 매매 판단 실행
        parent.chart_cache.data_updated.connect(parent.on_chart_data_updated_for_trading)

        # ===== 차트와 리스트 통합 =====
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
        parent.comboStg = QComboBox()
        parent.comboStg.setFixedWidth(200)
        strategyLayout.addWidget(parent.comboStg, alignment=Qt.AlignmentFlag.AlignLeft)
        strategyLayout.addStretch()
        parent.counterlabel = QLabel('타이머: 0')
        parent.counterlabel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        strategyLayout.addWidget(parent.counterlabel)
        parent.chart_status_label = QLabel("Chart: None")
        parent.chart_status_label.setProperty("class", "disconnected")
        parent.chart_status_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        strategyLayout.addWidget(parent.chart_status_label)

        # 매수 전략
        buyStrategyLayout = QHBoxLayout()
        buyStgLabel = QLabel("매수전략:")
        buyStgLabel.setFixedWidth(70)
        buyStrategyLayout.addWidget(buyStgLabel, alignment=Qt.AlignmentFlag.AlignLeft)
        parent.comboBuyStg = QComboBox()
        parent.comboBuyStg.setFixedWidth(200)
        buyStrategyLayout.addWidget(parent.comboBuyStg, alignment=Qt.AlignmentFlag.AlignLeft)
        buyStrategyLayout.addStretch()
        parent.saveBuyStgButton = QPushButton("수정")
        parent.saveBuyStgButton.setFixedWidth(100)
        buyStrategyLayout.addWidget(parent.saveBuyStgButton, alignment=Qt.AlignmentFlag.AlignRight)
        parent.buystgInputWidget = QTextEdit()
        parent.buystgInputWidget.setPlaceholderText("매수전략의 내용을 입력하세요...")
        parent.buystgInputWidget.setFixedHeight(80)

        # 매도 전략
        sellStrategyLayout = QHBoxLayout()
        sellStgLabel = QLabel("매도전략:")
        sellStgLabel.setFixedWidth(70)
        sellStrategyLayout.addWidget(sellStgLabel, alignment=Qt.AlignmentFlag.AlignLeft)
        parent.comboSellStg = QComboBox()
        parent.comboSellStg.setFixedWidth(200)
        sellStrategyLayout.addWidget(parent.comboSellStg, alignment=Qt.AlignmentFlag.AlignLeft)
        sellStrategyLayout.addStretch()
        parent.saveSellStgButton = QPushButton("수정")
        parent.saveSellStgButton.setFixedWidth(100)
        sellStrategyLayout.addWidget(parent.saveSellStgButton, alignment=Qt.AlignmentFlag.AlignRight)
        parent.sellstgInputWidget = QTextEdit()
        parent.sellstgInputWidget.setPlaceholderText("매도전략의 내용을 입력하세요...")
        parent.sellstgInputWidget.setFixedHeight(63)

        # 주식 현황 테이블
        parent.stock_table = QTableWidget()
        parent.stock_table.setRowCount(0)
        parent.stock_table.setColumnCount(6)
        parent.stock_table.setHorizontalHeaderLabels(["종목코드", "현재가", "보유수량", "매입단가", "손익금액", "손익률"])
        parent.stock_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        parent.stock_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        parent.stock_table.setFixedHeight(220)
        parent.stock_table.verticalHeader().setDefaultSectionSize(20)

        strategyAndTradeLayout.addLayout(strategyLayout)
        strategyAndTradeLayout.addLayout(buyStrategyLayout)
        strategyAndTradeLayout.addWidget(parent.buystgInputWidget)
        strategyAndTradeLayout.addLayout(sellStrategyLayout)
        strategyAndTradeLayout.addWidget(parent.sellstgInputWidget)
        strategyAndTradeLayout.addWidget(parent.stock_table)

        # ===== 터미널 출력 =====
        parent.terminalOutput = QTextEdit()
        parent.terminalOutput.setReadOnly(True)
        parent.terminalOutput.setProperty("class", "terminal")

        counterAndterminalLayout = QVBoxLayout()
        counterAndterminalLayout.addLayout(strategyAndTradeLayout)
        counterAndterminalLayout.addWidget(parent.terminalOutput)

        # ===== 메인 레이아웃 =====
        mainLayout = QHBoxLayout()
        mainLayout.addLayout(chartAndListLayout, 70)
        mainLayout.addLayout(counterAndterminalLayout, 30)
        parent.trading_tab.setLayout(mainLayout)

        # ===== 전략 콤보박스 초기화 =====
        parent.load_strategy_combos()
        
        # ===== 차트 드로어 초기화 (Plotly 기반) - 지연 초기화 =====
        parent.chartdrawer = None
        parent.chart_init_retry_count = 0
        parent.max_chart_init_retries = 3
        
        # ===== 이벤트 연결 =====
        parent.tradingModeCombo.currentIndexChanged.connect(parent.trading_mode_changed)
        parent.buycountButton.clicked.connect(parent.buycount_setting)
        parent.addStockButton.clicked.connect(parent.trading_manager.add_stock_to_list)
        # 연결 버튼 클릭 시 LoginHandler의 비동기 메서드를 직접 호출
        parent.connectButton.clicked.connect(
            lambda: self._safe_create_task(parent.login_handler._handle_connection_toggle_async()))
        parent.stockInputEdit.returnPressed.connect(parent.add_stock_to_list)

        parent.buyButton.clicked.connect(parent.buy_item)
        parent.deleteFirstButton.clicked.connect(parent.delete_select_item)
        parent.sellButton.clicked.connect(parent.sell_item)
        parent.sellAllButton.clicked.connect(parent.sell_all_item)

        # 리스트박스 이벤트 연결
        parent.monitoringBox.itemClicked.connect(parent.listBoxChanged)
        parent.boughtBox.itemClicked.connect(parent.listBoxChanged)
        logging.debug("✅ 리스트박스 클릭 이벤트 연결 완료")
        
        # 리스트박스 활성화
        parent.monitoringBox.setEnabled(True)
        parent.boughtBox.setEnabled(True)
        logging.debug("✅ 리스트박스 활성화 완료")
        
        parent.comboStg.currentIndexChanged.connect(parent.stgChanged)
        parent.comboBuyStg.currentIndexChanged.connect(parent.buyStgChanged)
        parent.comboSellStg.currentIndexChanged.connect(parent.sellStgChanged)
        parent.saveBuyStgButton.clicked.connect(parent.save_buystrategy)
        parent.saveSellStgButton.clicked.connect(parent.save_sellstrategy)
    
    def listBoxChanged(self, current):
        """리스트박스 클릭 이벤트 - 차트 표시"""
        
        parent = self.parent
        
        logging.debug(f"🔍 listBoxChanged 호출됨 - current: {current}")
        
        # 어떤 리스트박스에서 클릭이 발생했는지 확인
        sender = parent.sender()
        if sender == parent.monitoringBox:
            logging.debug("📊 모니터링 종목 박스에서 클릭됨")
        elif sender == parent.boughtBox:
            logging.debug("📊 보유 종목 박스에서 클릭됨")
        else:
            logging.debug("📊 알 수 없는 리스트박스에서 클릭됨")
        
        # 중복 호출 방지를 위한 락
        logging.debug("🔍 차트 그리기 락 획득 시도...")
        if not parent.chart_drawing_lock.acquire(blocking=False):
            logging.warning("📊 listBoxChanged is already running. Skipping duplicate call.")
            return
        logging.debug("✅ 차트 그리기 락 획득 성공")
        
        # ChartDrawer가 처리 중인지 확인
        if (hasattr(parent, 'chartdrawer') and parent.chartdrawer and 
            hasattr(parent.chartdrawer, '_is_processing') and parent.chartdrawer._is_processing):
            logging.warning(f"📊 ChartDrawer가 이미 차트를 생성 중입니다 ({parent.chartdrawer._processing_code}). 중복 실행 방지.")
            parent.chart_drawing_lock.release()
            return
        logging.debug("✅ ChartDrawer 처리 상태 확인 완료")
        
        try:
            if current:
                item_text = current.text()
                logging.debug(f"🔍 선택된 아이템 텍스트: {item_text}")
                
                # 리스트박스 상태 확인
                logging.debug(f"🔍 monitoringBox 아이템 수: {parent.monitoringBox.count()}")
                logging.debug(f"🔍 boughtBox 아이템 수: {parent.boughtBox.count()}")
                logging.debug(f"🔍 monitoringBox 현재 선택: {parent.monitoringBox.currentItem()}")
                logging.debug(f"🔍 boughtBox 현재 선택: {parent.boughtBox.currentItem()}")
                
                # "종목코드 - 종목명" 형식에서 종목코드와 종목명 추출
                parts = item_text.split(' - ')
                code = parts[0]
                name = parts[1] if len(parts) > 1 else f"종목{code}"  # Fallback (API 호출 제거)
                
                logging.debug(f"📊 종목 클릭됨: {item_text} -> 종목코드: {code}, 종목명: {name}")
                
                # 중복 클릭 방지: 같은 종목을 연속으로 클릭한 경우 무시
                if (hasattr(parent, '_last_clicked_code') and 
                    parent._last_clicked_code == code and 
                    hasattr(parent, '_last_click_time')):
                    current_time = time.time()
                    if current_time - parent._last_click_time < 1.0:  # 1초 내 중복 클릭
                        logging.debug(f"🔄 중복 클릭 방지: {code} (1초 내 재클릭)")
                        return
                
                # 마지막 클릭 정보 저장
                parent._last_clicked_code = code
                parent._last_click_time = time.time()
                
                # 실시간 차트 위젯 업데이트
                if hasattr(parent, 'realtime_chart_widget') and parent.realtime_chart_widget:
                    parent.realtime_chart_widget.set_current_code(code)
                    logging.debug(f"📊 실시간 차트 종목 변경: {code}")
            else:
                logging.debug("🔍 current가 None입니다 - 종목 선택 해제됨")
                if hasattr(parent, 'realtime_chart_widget') and parent.realtime_chart_widget:
                    parent.realtime_chart_widget.set_current_code(None)
        except Exception as ex:
            logging.error(f"리스트박스 변경 이벤트 처리 실패: {ex}")
        finally:
            # 처리 완료 후 락 해제
            parent.chart_drawing_lock.release()


class StrategyManager:
    """전략 로드/저장 관리 매니저"""
    
    def __init__(self, parent):
        self.parent = parent
    
    def load_strategy_combos(self):
        """전략 콤보박스에 settings.ini 값 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # 투자전략 콤보박스 로드
            self.parent.comboStg.clear()
            if config.has_section('STRATEGIES'):
                for key, value in config.items('STRATEGIES'):
                    if key.startswith('stg_') or key == 'stg_integrated':
                        self.parent.comboStg.addItem(value)
            
            # 기본 전략 설정
            if config.has_option('SETTINGS', 'last_strategy'):
                last_strategy = config.get('SETTINGS', 'last_strategy')
                index = self.parent.comboStg.findText(last_strategy)
                if index >= 0:
                    self.parent.comboStg.setCurrentIndex(index)
                    logging.debug(f"✅ 저장된 투자전략 복원: {last_strategy}")
                    
                    # 조건검색식인 경우는 조건검색 목록 로드 후 자동 실행됨
                    if last_strategy.startswith("[조건검색]"):
                        logging.debug("🔍 저장된 조건검색식 발견 - 조건검색 목록 로드 후 자동 실행 예정")
                else:
                    logging.warning(f"⚠️ 저장된 투자전략을 찾을 수 없습니다: {last_strategy}")
            else:
                logging.debug("저장된 투자전략이 없습니다. 기본 전략을 사용합니다.")
            
            # 매수전략 콤보박스 로드
            self.load_buy_strategies()
            
            # 매도전략 콤보박스 로드
            self.load_sell_strategies()
            
            # 초기 전략 내용 로드
            self.parent.load_initial_strategy_content()
            
            logging.debug("투자전략 콤보박스 로드 완료")
            
        except Exception as ex:
            logging.error(f"전략 콤보박스 로드 실패: {ex}")
    
    def load_buy_strategies(self):
        """매수전략 콤보박스 로드"""
        self._load_strategy_list(self.parent.comboBuyStg, 'buy_stg_', 'buy')
    
    def load_sell_strategies(self):
        """매도전략 콤보박스 로드"""
        self._load_strategy_list(self.parent.comboSellStg, 'sell_stg_', 'sell')
    
    def _load_strategy_list(self, combo_widget, key_prefix, strategy_type):
        """전략 목록을 콤보박스에 로드"""
        try:
            combo_widget.clear()
            
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # 현재 선택된 투자전략 가져오기
            current_strategy = self.parent.comboStg.currentText()
            if not current_strategy:
                logging.warning("선택된 투자전략이 없습니다")
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
                            logging.warning(f"전략 파싱 실패: {section}.{key}")
            else:
                # 해당 전략 섹션 확인
                if not config.has_section(current_strategy):
                    logging.warning(f"settings.ini에 [{current_strategy}] 섹션이 없습니다")
                    return
                
                # 전략 목록 추출
                for key in config[current_strategy]:
                    if key.startswith(key_prefix):
                        try:
                            strategy_data = json.loads(config[current_strategy][key])
                            strategies.append((key, strategy_data.get('name', key)))
                        except json.JSONDecodeError:
                            logging.warning(f"전략 파싱 실패: {key}")
            
            # 콤보박스에 추가
            for key, name in strategies:
                combo_widget.addItem(name, key)
            
            logging.debug(f"{strategy_type} 전략 {len(strategies)}개 로드 완료")
            
        except Exception as ex:
            logging.error(f"전략 목록 로드 실패 ({strategy_type}): {ex}")
    
    def save_current_strategy(self):
        """현재 선택된 투자전략을 settings.ini에 저장"""
        try:
            current_strategy = self.parent.comboStg.currentText()
            if not current_strategy:
                logging.debug("저장할 투자전략이 없습니다")
                return
            
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # [Strategy] 섹션 대신 [SETTINGS].last_strategy에 통합 저장
            if not config.has_section('SETTINGS'):
                config.add_section('SETTINGS')
            config.set('SETTINGS', 'last_strategy', current_strategy)
            
            with open('settings.ini', 'w', encoding='utf-8') as f:
                config.write(f)
            
            logging.debug(f"✅ 현재 투자전략 저장(SETTINGS.last_strategy): {current_strategy}")
            
        except Exception as ex:
            logging.error(f"투자전략 저장 실패: {ex}")
    
    def load_initial_strategy_content(self):
        """초기 전략 내용을 텍스트박스에 로드"""
        try:
            # 매수전략 초기 내용 로드
            if self.parent.comboBuyStg.count() > 0:
                current_buy_strategy = self.parent.comboBuyStg.currentText()
                self.load_strategy_content(current_buy_strategy, 'buy')
            
            # 매도전략 초기 내용 로드
            if self.parent.comboSellStg.count() > 0:
                current_sell_strategy = self.parent.comboSellStg.currentText()
                self.load_strategy_content(current_sell_strategy, 'sell')
                
        except Exception as ex:
            logging.error(f"초기 전략 내용 로드 실패: {ex}")
    
    def load_strategy_content(self, strategy_name, strategy_type):
        """전략 내용을 텍스트 위젯에 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            current_strategy = self.parent.comboStg.currentText()

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
                    self.parent.buystgInputWidget.setPlainText(content)
                elif strategy_type == 'sell':
                    self.parent.sellstgInputWidget.setPlainText(content)
                    
        except Exception as ex:
            logging.error(f"전략 내용 로드 실패: {ex}")
    
    def stg_changed(self):
        """전략 변경 이벤트 핸들러"""
        try:
            strategy_name = self.parent.comboStg.currentText()
            logging.debug(f"투자 전략 변경: {strategy_name}")
            
            # 현재 선택된 전략을 settings.ini에 저장
            self.save_current_strategy()
            
            # 조건검색식인지 확인 (조건검색 목록에 있는지 확인)
            if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                condition_names = [condition['title'] for condition in self.parent.condition_search_list]
                if strategy_name in condition_names:
                    # 조건검색식 선택 시 바로 실행 (비동기)
                    try:
                        asyncio.get_running_loop()  # 루프 확인
                        asyncio.create_task(self.parent.handle_condition_search())
                    except RuntimeError:
                        logging.warning("⚠️ 이벤트 루프가 없어 조건검색을 실행할 수 없습니다")
                    return
            
            # 통합 전략인 경우 모든 조건검색식 실행
            if strategy_name == "통합 전략":
                if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                    logging.debug("🔍 통합 전략 실행: 모든 조건검색식 적용 (ConditionSearchManager)")
                    try:
                        asyncio.get_running_loop()  # 루프 확인
                        asyncio.create_task(self.parent.condition_search_manager.handle_integrated_condition_search())
                    except RuntimeError:
                        logging.warning("⚠️ 이벤트 루프가 없어 통합 전략을 실행할 수 없습니다")
                    return
            
            # 일반 투자전략인 경우 기존 로직 실행
            # 투자전략 변경 시 매수/매도 전략도 업데이트
            self.load_buy_strategies()
            self.load_sell_strategies()
            
            # 변경된 전략의 첫 번째 매수/매도 전략 내용 자동 로드
            self.load_initial_strategy_content()
            
        except Exception as ex:
            logging.error(f"전략 변경 실패: {ex}")
    
    def buy_stg_changed(self):
        """매수 전략 변경 이벤트 핸들러"""
        try:
            strategy_name = self.parent.comboBuyStg.currentText()
            logging.debug(f"매수 전략 변경: {strategy_name}")
            
            # 매수 전략 내용을 텍스트 위젯에 표시
            self.load_strategy_content(strategy_name, 'buy')
            
        except Exception as ex:
            logging.error(f"매수 전략 변경 실패: {ex}")
    
    def sell_stg_changed(self):
        """매도 전략 변경 이벤트 핸들러"""
        try:
            strategy_name = self.parent.comboSellStg.currentText()
            logging.debug(f"매도 전략 변경: {strategy_name}")
            
            # 매도 전략 내용을 텍스트 위젯에 표시
            self.load_strategy_content(strategy_name, 'sell')
            
        except Exception as ex:
            logging.error(f"매도 전략 변경 실패: {ex}")
    
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
            current_strategy = self.parent.comboStg.currentText()
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
                    logging.error(f"통합 전략 저장 오류: 잘못된 키 형식 - {key_from_combobox}")
                    return

            if not config.has_section(target_section):
                logging.error(f"{strategy_type} 전략 저장 실패: 섹션을 찾을 수 없음 - [{target_section}]")
                return

            if not config.has_option(target_section, target_key):
                logging.error(f"{strategy_type} 전략 저장 실패: 키를 찾을 수 없음 - {target_key}")
                return

            # 기존 전략 데이터 로드 및 수정
            try:
                strategy_json_str = config.get(target_section, target_key)
                strategy_data = json.loads(strategy_json_str)
                strategy_data['content'] = strategy_text
                # json.dumps를 사용하여 문자열로 변환
                config.set(target_section, target_key, json.dumps(strategy_data, ensure_ascii=False))
            except (json.JSONDecodeError, KeyError) as e:
                logging.error(f"전략 데이터 파싱 또는 수정 실패: {e}")
                return

            # 파일 저장
            with open('settings.ini', 'w', encoding='utf-8') as configfile:
                config.write(configfile)

            logging.debug(f"{strategy_type} 전략 '{current_strategy_name}'이 저장되었습니다.")

            # 전략 즉시 반영: KiwoomStrategy 객체의 설정을 다시 로드
            if hasattr(self.parent, 'objstg') and self.parent.objstg:
                self.parent.objstg.load_strategy_config()
                logging.info(f"✅ {strategy_type} 전략이 즉시 반영되었습니다.")

        except Exception as ex:
            logging.error(f"{strategy_type} 전략 저장 실패: {ex}")
    
    def save_buystrategy(self):
        """매수 전략 저장"""
        self._save_strategy(self.parent.buystgInputWidget, self.parent.comboBuyStg, 'buy_stg_', '매수')
    
    def save_sellstrategy(self):
        """매도 전략 저장"""
        self._save_strategy(self.parent.sellstgInputWidget, self.parent.comboSellStg, 'sell_stg_', '매도')


class TradingManager:
    """매매 실행 관리 매니저"""
    
    def __init__(self, parent):
        self.parent = parent
    
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
            logging.error(f"target_buy_count 읽기 실패: {ex}")
            return 3  # 기본값

    def buycount_setting(self):
        """투자 종목수 설정"""
        try:
            buycount = int(self.parent.buycountEdit.text())
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
                
                logging.info(f"✅ 최대 투자 종목수 설정 완료: {buycount}종목")
                QMessageBox.information(self.parent, "설정 완료", f"최대 투자 종목수가 {buycount}종목으로 설정되었습니다.")
            else:
                logging.warning("1 이상의 숫자를 입력해주세요.")
                QMessageBox.warning(self.parent, "입력 오류", "1 이상의 숫자를 입력해주세요.")
        except ValueError:
            logging.warning("올바른 숫자를 입력해주세요.")
            QMessageBox.warning(self.parent, "입력 오류", "올바른 숫자를 입력해주세요.")
        except Exception as ex:
            logging.error(f"투자 종목수 설정 실패: {ex}")
            QMessageBox.critical(self.parent, "설정 실패", f"설정 중 오류가 발생했습니다:\n{ex}")
    
    def delete_select_item(self):
        """선택된 종목 삭제"""
        try:
            current_item = self.parent.monitoringBox.currentItem()
            if current_item:
                self.parent.monitoringBox.takeItem(self.parent.monitoringBox.row(current_item))
                logging.debug("선택된 종목이 삭제되었습니다.")
            else:
                logging.warning("삭제할 종목을 선택해주세요.")
        except Exception as ex:
            logging.error(f"종목 삭제 실패: {ex}")
    
    def add_stock_to_list(self):
        """투자 대상 종목 리스트에 종목 추가 (API 큐를 통한 차트 데이터 수집 후 추가)"""
        try:
            stock_input = self.parent.stockInputEdit.text().strip()
            if not stock_input:
                logging.warning("종목명 또는 종목코드를 입력해주세요.")
                return
            
            # 종목코드 정규화 (6자리 숫자로 변환)
            stock_code, stock_name = self.parent.normalize_stock_input(stock_input)
            
            # 종목명 검색 실패 시 처리
            if stock_code is None or stock_name is None:
                logging.error(f"❌ 종목을 찾을 수 없습니다: {stock_input}")
                return
            
            # 이미 모니터링에 존재하는지 확인
            for i in range(self.parent.monitoringBox.count()):
                existing_code = self.parent.monitoringBox.item(i).text()
                if existing_code == stock_code:
                    logging.warning(f"'{stock_name}' 종목이 이미 모니터링에 존재합니다.")
                    return
            
            # 입력 필드 초기화
            self.parent.stockInputEdit.clear()
            
            # API 큐에 추가 (차트 데이터 수집 후 모니터링에 추가)
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                if self.parent.chart_cache.add_stock_to_api_queue(stock_code):
                    logging.debug(f"📋 수동 추가 종목을 API 큐에 추가: {stock_code}")
                    logging.debug("📋 차트 데이터 수집 완료 후 모니터링에 추가됩니다")
                else:
                    logging.warning(f"⚠️ API 큐 추가 실패: {stock_code}")
            else:
                logging.error("❌ chart_cache가 없어 종목을 추가할 수 없습니다")
            
        except Exception as ex:
            logging.error(f"종목 추가 실패: {ex}")
    
    def trading_mode_changed(self):
        """거래 모드 변경 이벤트 핸들러"""
        try:
            mode = "모의투자" if self.parent.tradingModeCombo.currentIndex() == 0 else "실제투자"
            logging.debug(f"거래 모드 변경: {mode}")
            
            # 키움 클라이언트의 is_mock 설정 업데이트
            if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'kiwoom_client') and self.parent.login_handler.kiwoom_client:
                is_mock = (self.parent.tradingModeCombo.currentIndex() == 0)
                self.parent.login_handler.kiwoom_client.is_mock = is_mock
                logging.debug(f"키움 클라이언트 모의투자 설정 업데이트: {is_mock}")
            
            # 연결된 상태라면 재연결 안내 (로그로만 표시)
            if hasattr(self.parent, 'trader') and self.parent.trader and self.parent.trader.client and self.parent.trader.client.is_connected:
                logging.debug(f"거래 모드가 {mode}로 변경되었습니다. 새로운 설정을 적용하려면 API를 재연결해주세요.")
                
        except Exception as ex:
            logging.error(f"거래 모드 변경 실패: {ex}")
    
    def sell_all_item(self):
        """전체 매도 (키움 REST API 기반)"""
        try:
            if self.parent.boughtBox.count() > 0:
                # 확인 대화상자
                reply = QMessageBox.question(self.parent, "전체 매도 확인", 
                                           "보유 중인 모든 종목을 매도하시겠습니까?",
                                           QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                
                if reply == QMessageBox.StandardButton.Yes:
                    logging.info("🔄 전체 매도 시작")
                    
                    # 보유 종목 목록 생성
                    sell_items = []
                    for i in range(self.parent.boughtBox.count()):
                        item = self.parent.boughtBox.item(i)
                        item_text = item.text()
                        code = item_text.split(' - ')[0] if ' - ' in item_text else item_text.split(' ')[0]
                        name = item_text.split(' - ')[1] if ' - ' in item_text else "알 수 없음"
                        sell_items.append((code, name))
                    
                    # 각 종목에 대해 매도 주문 실행
                    success_count = 0
                    for code, name in sell_items:
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
                                    logging.debug(f"💰 웹소켓 잔고: {code} {quantity}주")
                            
                            # 2차: 웹소켓 데이터가 없거나 수량이 0이면 REST API로 조회
                            if quantity <= 0:
                                try:
                                    if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                                        balance_result = self.parent.login_handler.kiwoom_client.get_acnt_balance()
                                        if balance_result:
                                            holdings = balance_result.get('stk_acnt_evlt_prst', balance_result.get('output1', []))
                                            for stock in holdings:
                                                raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                                                stock_code = self.parent.normalize_stock_code(raw_code)
                                                if stock_code == code:
                                                    quantity = self.parent.data_manager.safe_int(stock.get('rmnd_qty', stock.get('hldg_qty', 0)))
                                                    logging.debug(f"📡 REST API 잔고: {code} {quantity}주")
                                                    break
                                except Exception as api_ex:
                                    logging.error(f"❌ REST API 잔고 조회 실패: {api_ex}")
                            
                            # 수량 확인
                            if quantity <= 0:
                                logging.warning(f"⚠️ {code} 보유 수량 없음 - 건너뜀")
                                continue
                            
                            # 매도 주문 실행
                            if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                                success = self.parent.login_handler.kiwoom_client.place_sell_order(code, quantity, 0, "market")
                                
                                if success:
                                    success_count += 1
                                    logging.info(f"✅ 전체 매도 성공: {code} {quantity}주")
                                else:
                                    logging.error(f"❌ 전체 매도 실패: {code}")
                        except Exception as item_ex:
                            logging.error(f"❌ {code} 매도 중 오류: {item_ex}")
                    
                    # 결과 로그
                    if success_count > 0:
                        logging.info(f"✅ 전체 매도 완료: {success_count}개 종목")
                    else:
                        logging.error("❌ 전체 매도 실패")
                        QMessageBox.warning(self.parent, "전체 매도 실패", 
                                          "매도 주문이 실패했습니다.")
                else:
                    logging.debug("전체 매도 취소됨")
            else:
                logging.warning("매도할 종목이 없습니다.")
        except Exception as ex:
            logging.error(f"전체 매도 실패: {ex}")
            QMessageBox.critical(self.parent, "전체 매도 오류", f"전체 매도 중 오류가 발생했습니다: {ex}")
    
    def sell_item(self):
        """종목 매도 - 보유수량 전량 매도 (키움 REST API 기반)"""
        try:
            current_item = self.parent.boughtBox.currentItem()
            if current_item:
                item_text = current_item.text()
                # "종목코드 - 종목명" 형식에서 종목코드 추출
                code = item_text.split(' - ')[0] if ' - ' in item_text else item_text
                
                logging.debug(f"매도 요청: {code}")
                
                quantity = 0
                
                # 1차: REST API로 주문가능수량 조회 (kt00004)
                logging.info(f"📡 REST API로 주문가능수량 조회 시도: {code}")
                try:
                    if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                        balance_result = self.parent.login_handler.kiwoom_client.get_acnt_balance()
                        if balance_result:
                            holdings = balance_result.get('stk_acnt_evlt_prst', balance_result.get('output1', []))
                            for stock in holdings:
                                raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                                stock_code = self.parent.data_manager.normalize_stock_code(raw_code)
                                if stock_code == code:
                                    quantity = self.parent.data_manager.safe_int(stock.get('rmnd_qty', 0))
                                    logging.info(f"✅ REST API로 주문가능수량 조회 성공: {code} {quantity}주")
                                    break
                except Exception as api_ex:
                    logging.error(f"❌ REST API 잔고 조회 실패: {api_ex}")

                # 2차: REST API 조회 실패 또는 수량 0일 때 웹소켓 데이터로 재확인 (Fallback)
                if quantity <= 0:
                    if (hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'websocket_client')):
                        ws_balance_data = self.parent.login_handler.websocket_client.balance_data
                        if ws_balance_data and code in ws_balance_data:
                            quantity = ws_balance_data[code].get('order_available_qty', 0)
                            logging.info(f"💰 웹소켓 잔고 조회 (Fallback): {code} 주문가능수량 {quantity}주")
                
                # 최종 수량 확인
                if quantity <= 0:
                    logging.warning(f"⚠️ 보유 수량 없음: {code}")
                    QMessageBox.warning(self.parent, "매도 불가", f"{code} 보유 수량이 없습니다.\n웹소켓과 REST API 모두 확인했습니다.")
                    return
                
                logging.info(f"💰 전량 매도 실행: {code} {quantity}주")
                
                # 시장가 매도 주문 (전량)
                if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                    success = self.parent.login_handler.kiwoom_client.place_sell_order(code, quantity, 0, "market")
                    
                    if success:
                        # 매도 성공 (실시간 잔고 데이터가 자동으로 보유 종목에서 제거됨)
                        logging.info(f"✅ 매도 주문 성공: {code} {quantity}주 전량 매도")
                    else:
                        logging.error(f"❌ 매도 주문 실패: {code}")
                        QMessageBox.warning(self.parent, "매도 실패", f"{code} 매도 주문이 실패했습니다.")
                else:
                    logging.error("키움 클라이언트가 초기화되지 않았습니다")
                    QMessageBox.warning(self.parent, "오류", "키움 클라이언트가 초기화되지 않았습니다.")
            else:
                logging.warning("매도할 종목을 선택해주세요.")
                QMessageBox.warning(self.parent, "선택 오류", "매도할 종목을 선택해주세요.")
        except Exception as ex:
            logging.error(f"매도 실패: {ex}")
            logging.error(f"매도 실패 상세: {traceback.format_exc()}")
            QMessageBox.critical(self.parent, "매도 오류", f"매도 중 오류가 발생했습니다: {ex}")
    
    def buy_item(self):
        """종목 매입 - 자동 매입가능수량 계산 (키움 REST API 기반)"""
        try:
            current_item = self.parent.monitoringBox.currentItem()
            if current_item:
                code = current_item.text()
               
                logging.info(f"🛒 매입 요청: {code}")
                
                # 보유 종목 확인 (이미 보유 중인 종목은 매수 제외)
                if hasattr(self.parent, 'boughtBox'):
                    for i in range(self.parent.boughtBox.count()):
                        item_code = self.parent.boughtBox.item(i).text()
                        if item_code == code:
                            logging.info(f"⚠️ 매수 주문 취소: {code}는 이미 보유 중인 종목입니다.")
                            QMessageBox.warning(self.parent, "매수 불가", f"{code}는 이미 보유 중인 종목입니다.")
                            return
                
                # 자동 매수 수량 계산
                quantity = 0
                
                try:
                    # 1단계: 투자가능금액 조회
                    if not hasattr(self.parent, 'trader') or not self.parent.trader:
                        logging.error("⚠️ trader가 초기화되지 않았습니다 (API 연결이 필요합니다)")
                        QMessageBox.warning(self.parent, "오류", "API에 먼저 연결해주세요.")
                        return
                
                    available_cash = self.parent.trader.get_available_cash()
                    
                    if available_cash <= 0:
                        logging.warning(f"⚠️ 매수 주문 취소: 투자가능금액 부족 ({available_cash:,.0f}원)")
                        QMessageBox.warning(self.parent, "매수 불가", f"투자가능금액이 부족합니다.\n현재: {available_cash:,.0f}원")
                        return
                    
                    # 2단계: 매수가능 종목수 조회
                    available_buy_count = self.parent.login_handler.get_available_buy_count()
                    
                    if available_buy_count <= 0:
                        logging.warning(f"⚠️ 매수 주문 취소: 최대 보유 종목 수 도달")
                        QMessageBox.warning(self.parent, "매수 불가", "최대 보유 종목 수에 도달했습니다.")
                        return
                    
                    # 3단계: 현재가 조회 (캐시 데이터 우선, 없으면 REST API, 실패 시 추정)
                    current_price = 0
                    price_source = ""
                    
                    # 캐시 데이터에서 현재가 조회 시도
                    if hasattr(self.parent.login_handler, 'websocket_client') and self.parent.login_handler.websocket_client:
                        ws_client = self.parent.login_handler.websocket_client
                        if hasattr(ws_client, 'chart_cache'):
                            tic_data = ws_client.chart_cache.get_tic_chart(code)
                            if tic_data and tic_data.get('close') and len(tic_data['close']) > 0:
                                current_price = float(tic_data['close'][-1])
                                price_source = "캐시"
                    
                    # 캐시에 없으면 REST API로 현재가 조회
                    if current_price <= 0:
                        try:
                            current_price = self.parent.trader.get_current_price(code)
                            if current_price > 0:
                                price_source = "API"
                        except Exception as price_ex:
                            logging.debug(f"현재가 조회 실패: {price_ex}")
                    
                    # 현재가 조회 실패 시 추정 현재가 사용 (시장가 주문용)
                    if current_price <= 0:
                        current_price = 50000
                        price_source = "추정"
                        logging.debug(f"현재가 조회 실패 → 추정가 사용")
                    
                    # 4단계: 매수 수량 계산
                    # 한 종목당 투자예산 = 투자가능금액 / 매수가능종목수
                    budget_per_stock = available_cash // available_buy_count
                    quantity = int(budget_per_stock / current_price)
                    
                    # 최소 1주는 매수하도록 보장
                    if quantity <= 0:
                        quantity = 1
                    
                    logging.info(f"🛒 {code} 매수: {quantity}주 @ 시장가 (예산 {budget_per_stock:,.0f}원, 현재가 {current_price:,.0f}원/{price_source})")
                    
                except Exception as calc_ex:
                    logging.error(f"❌ 매수 수량 계산 실패: {calc_ex}")
                    QMessageBox.warning(self.parent, "오류", f"매수 수량 계산 중 오류가 발생했습니다:\n{calc_ex}")
                    return
                
                # 시장가 매수 주문
                price = 0  # 시장가로 고정
                
                # 키움 REST API를 통한 매수 주문 (시장가만)
                if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                    success = self.parent.login_handler.kiwoom_client.place_buy_order(code, quantity, 0, "market")
                    
                    if success:
                        logging.info(f"✅ 매수 주문 성공: {code} {quantity}주")
                    else:
                        logging.error(f"❌ 매수 주문 실패: {code}")
                        QMessageBox.warning(self.parent, "매수 실패", f"{code} 매수 주문이 실패했습니다.")
                else:
                    logging.error("키움 클라이언트가 초기화되지 않았습니다")
                    QMessageBox.warning(self.parent, "오류", "키움 클라이언트가 초기화되지 않았습니다.")
            else:
                logging.warning("매입할 종목을 선택해주세요.")
                QMessageBox.warning(self.parent, "선택 오류", "매입할 종목을 선택해주세요.")
        except Exception as ex:
            logging.error(f"매입 실패: {ex}")
            QMessageBox.critical(self.parent, "매입 오류", f"매입 중 오류가 발생했습니다:\n{ex}")
    
    def on_chart_data_updated_for_trading(self, code):
        """차트 데이터 업데이트 시 AutoTrader에 매매 판단 위임"""
        try:
            # AutoTrader에서 매매 판단 및 실행 (구조 개선: 모든 매매 로직이 AutoTrader에 통합)
            if hasattr(self.parent, 'autotrader') and self.parent.autotrader:
                self.parent.autotrader.analyze_and_execute_trading(code)
            else:
                logging.warning("⚠️ AutoTrader 객체가 없어 매매 판단을 건너뜁니다")
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 매매 판단 위임 실패: {code} - {ex}")


class BacktestManager:
    """백테스팅 관리 매니저"""
    
    def __init__(self, parent):
        self.parent = parent
    
    def init_backtest_tab(self):
        """백테스팅 탭 초기화"""
        
        parent = self.parent
        
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
        parent.bt_start_date = QLineEdit()
        parent.bt_start_date.setPlaceholderText("YYYYMMDD")
        parent.bt_start_date.setFixedWidth(120)
        period_layout.addWidget(parent.bt_start_date)
        
        # 종료일
        period_layout.addWidget(QLabel("종료일:"))
        parent.bt_end_date = QLineEdit()
        parent.bt_end_date.setPlaceholderText("YYYYMMDD")
        parent.bt_end_date.setFixedWidth(120)
        period_layout.addWidget(parent.bt_end_date)
        
        # DB 기간 불러오기 버튼
        parent.bt_load_period_button = QPushButton("DB 기간 불러오기")
        parent.bt_load_period_button.setFixedWidth(150)
        parent.bt_load_period_button.clicked.connect(parent.load_db_period)
        period_layout.addWidget(parent.bt_load_period_button)
        
        period_group.setLayout(period_layout)
        settings_layout.addWidget(period_group)
        
        # 중간 스트레치
        settings_layout.addStretch(1)
        
        # 초기 자금
        settings_layout.addWidget(QLabel("초기 자금:"))
        parent.bt_initial_cash = QLineEdit("10000000")
        parent.bt_initial_cash.setFixedWidth(120)
        settings_layout.addWidget(parent.bt_initial_cash)
        settings_layout.addStretch(1)
        
        # 전략 선택
        settings_layout.addWidget(QLabel("투자 전략:"))
        parent.bt_strategy_combo = QComboBox()
        parent.bt_strategy_combo.setFixedWidth(120)
        settings_layout.addWidget(parent.bt_strategy_combo)
        settings_layout.addStretch(1)
        
        # 백테스팅 전략 콤보박스 로드
        parent.load_backtest_strategies()
        
        # 실행 버튼
        parent.bt_run_button = QPushButton("백테스팅 실행")
        parent.bt_run_button.setFixedWidth(120)
        parent.bt_run_button.clicked.connect(parent.run_backtest)
        settings_layout.addWidget(parent.bt_run_button)
        
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
        parent.bt_results_text = QTextEdit()
        parent.bt_results_text.setReadOnly(True)
        parent.bt_results_text.setMaximumWidth(450)
        left_layout.addWidget(parent.bt_results_text)
        
        left_widget.setLayout(left_layout)
        
        # 오른쪽: 차트
        right_widget = QWidget()
        right_layout = QVBoxLayout()
        
        # 백테스팅 차트는 현재 비활성화
        # parent.bt_fig = Figure(figsize=(10, 8))
        # parent.bt_canvas = FigureCanvas(parent.bt_fig)
        # right_layout.addWidget(parent.bt_canvas)
        
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
        parent.bt_daily_table = QTableWidget()
        parent.bt_daily_table.setColumnCount(8)
        parent.bt_daily_table.setHorizontalHeaderLabels([
            "날짜", "일손익", "수익률(%)", "거래수", "승", "패", "누적손익", "포트폴리오"
        ])
        parent.bt_daily_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        parent.bt_daily_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        parent.bt_daily_table.setMaximumWidth(600)
        daily_left_layout.addWidget(parent.bt_daily_table)
        
        daily_left_widget.setLayout(daily_left_layout)
        
        # 오른쪽: 일별 차트
        daily_right_widget = QWidget()
        daily_right_layout = QVBoxLayout()
        
        # 백테스팅 일봉 차트는 현재 비활성화
        # parent.bt_daily_fig = Figure(figsize=(10, 8))
        # parent.bt_daily_canvas = FigureCanvas(parent.bt_daily_fig)
        # daily_right_layout.addWidget(parent.bt_daily_canvas)
        
        daily_right_widget.setLayout(daily_right_layout)
        
        daily_layout.addWidget(daily_left_widget, 1)
        daily_layout.addWidget(daily_right_widget, 2)
        daily_tab.setLayout(daily_layout)
        
        # 탭 추가
        results_tab_widget.addTab(overall_tab, "전체 성과")
        results_tab_widget.addTab(daily_tab, "일별 성과")
        
        layout.addWidget(results_tab_widget)
        
        parent.backtest_tab.setLayout(layout)
    
    def load_backtest_strategies(self):
        """백테스팅 전략 콤보박스 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            self.parent.bt_strategy_combo.clear()
            if config.has_section('STRATEGIES'):
                for key, value in config.items('STRATEGIES'):
                    if key.startswith('stg_') or key == 'stg_integrated':
                        self.parent.bt_strategy_combo.addItem(value)
            
            # 기본 전략 설정
            if config.has_option('SETTINGS', 'last_strategy'):
                last_strategy = config.get('SETTINGS', 'last_strategy')
                index = self.parent.bt_strategy_combo.findText(last_strategy)
                if index >= 0:
                    self.parent.bt_strategy_combo.setCurrentIndex(index)
            
            logging.debug("백테스팅 전략 콤보박스 로드 완료")
            
        except Exception as ex:
            logging.error(f"백테스팅 전략 콤보박스 로드 실패: {ex}")
    
    def load_db_period(self):
        """DB 기간 불러오기"""
        try:
            # KiwoomBacktester 인스턴스를 생성하여 DB 경로를 올바르게 참조
            # DB 파일 경로는 backtester.py에 정의되어 있음
            backtester = KiwoomBacktester(db_path='stock_data.db')
            start_date, end_date = backtester.get_db_data_range()

            if start_date and end_date:
                self.parent.bt_start_date.setText(start_date)
                self.parent.bt_end_date.setText(end_date)
                logging.debug(f"DB 기간 로드: {start_date} ~ {end_date}")
            else:
                logging.warning("DB에서 날짜 정보를 찾을 수 없습니다.")
                
        except Exception as ex:
            logging.error(f"DB 기간 로드 실패: {ex}")
    
    def run_backtest(self):
        """백테스팅 실행"""
        try:
            # 1. KiwoomBacktester 인스턴스 생성 (기간 조회를 위해 먼저 생성)
            backtester = KiwoomBacktester(db_path='stock_data.db')

            # 2. DB에서 실제 데이터 기간 자동 조회
            start_date, end_date = backtester.get_db_data_range()
            if not start_date or not end_date:
                QMessageBox.warning(self.parent, "데이터 없음", "DB에서 백테스팅을 위한 데이터 기간을 찾을 수 없습니다.")
                return

            # 3. UI에 조회된 기간 설정 및 파라미터 가져오기
            self.parent.bt_start_date.setText(start_date)
            self.parent.bt_end_date.setText(end_date)
            initial_cash = int(self.parent.bt_initial_cash.text())
            strategy_name = self.parent.bt_strategy_combo.currentText()
            backtester.initial_cash = initial_cash # 초기 자금 설정

            if not all([start_date, end_date, strategy_name]):
                QMessageBox.warning(self.parent, "입력 오류", "시작일, 종료일, 투자 전략을 모두 선택해주세요.")
                return

            logging.info(f"백테스팅 시작: {strategy_name} ({start_date} ~ {end_date})")
            self.parent.bt_results_text.clear()
            self.parent.bt_results_text.append(f"백테스팅을 시작합니다: {strategy_name}\n")
            QApplication.processEvents()

            # 4. 백테스팅 대상 종목 가져오기 (모니터링 중인 종목 사용)
            codes = self.parent.get_monitoring_stock_codes()
            if not codes:
                QMessageBox.warning(self.parent, "오류", "백테스팅을 실행할 모니터링 대상 종목이 없습니다.")
                return

            self.parent.bt_results_text.append(f"대상 종목: {', '.join(codes)}\n")
            QApplication.processEvents()

            # 5. 백테스팅 실행
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
                self.parent.bt_results_text.append("\n=== 백테스팅 결과 ===\n" + summary)
                backtester.plot_results(strategy_name)
                backtester.export_results(strategy_name)
            else:
                self.parent.bt_results_text.append("\n백테스팅 실행에 실패했거나 결과가 없습니다.")

        except Exception as ex:
            logging.error(f"백테스팅 실행 실패: {ex}")
            QMessageBox.critical(self.parent, "백테스팅 오류", f"백테스팅 실행 중 오류가 발생했습니다:\n{ex}")


class AccountManager:
    """계좌 조회 및 잔고 관리 매니저"""
    
    def __init__(self, parent: 'MyWindow'):
        self.parent = parent
    
    def handle_acnt_balance_query(self):
        """계좌 잔고조회 - REST API로 초기 조회 후 실시간 업데이트
        
        1. REST API로 초기 잔고 조회 및 보유종목 리스트 생성
        2. 웹소켓 실시간 잔고(04)로 변동사항 추적
        """
        
        parent = self.parent
        
        try:
            logging.debug("🔧 계좌 잔고 조회 시작 (REST API)")
            
            if not hasattr(parent, 'trader') or not parent.trader:
                logging.warning("⚠️ 트레이더가 초기화되지 않았습니다")
                return
            
            if not hasattr(parent.trader, 'client') or not parent.trader.client:
                logging.warning("⚠️ API 클라이언트가 연결되지 않았습니다")
                return

            # 1. 예수금상세현황 조회 (kt00001)
            logging.debug("🔍 예수금상세현황 조회 중...")
            try:
                deposit_data = parent.trader.client.get_deposit_detail()
                if deposit_data:
                    logging.debug("✅ 예수금상세현황 조회 성공")
                    parent._display_deposit_info(deposit_data)
                else:
                    logging.warning("⚠️ 예수금상세현황 조회 실패")
            except Exception as deposit_ex:
                logging.error(f"❌ 예수금상세현황 조회 실패: {deposit_ex}")

            # 2. REST API 잔고조회 (kt00004) - 초기 보유종목 확인
            logging.debug("🔍 계좌 잔고 조회 중...")
            try:
                balance_data = parent.trader.client.get_acnt_balance()
                if balance_data:
                    # 키움 API 공식 문서 기준 필드명 사용
                    # stk_acnt_evlt_prst: 종목별계좌평가현황 (LIST)
                    holdings = balance_data.get('stk_acnt_evlt_prst', balance_data.get('output1', []))
                    
                    if holdings and len(holdings) > 0:
                        logging.info(f"📦 보유 종목 수: {len(holdings)}개")
                        logging.info("=" * 70)
                        logging.info("📋 보유 종목 목록 (REST API)")
                        logging.info("-" * 70)
                        
                        for stock in holdings:
                            # 키움 API 공식 문서 기준 필드명 (구 버전 호환)
                            raw_code = stock.get('stk_cd', stock.get('pdno', '알 수 없음'))
                            stock_code = parent.data_manager.normalize_stock_code(raw_code)  # A 접두사 제거
                            stock_name = stock.get('stk_nm', stock.get('prdt_name', '알 수 없음'))
                            quantity = parent.data_manager.safe_int(stock.get('rmnd_qty', stock.get('hldg_qty', 0)))
                            current_price = parent.data_manager.safe_int(stock.get('cur_prc', stock.get('prpr', 0)))
                            avg_price = parent.data_manager.safe_int(stock.get('avg_prc', stock.get('pchs_avg_pric', 0)))
                            profit_loss = parent.data_manager.safe_int(stock.get('pl_amt', stock.get('evlu_pfls_amt', 0)))
                            profit_rate = parent.data_manager.safe_float(stock.get('pl_rt', stock.get('evlu_pfls_rt', 0)))
                            
                            if quantity > 0:
                                logging.info(f"  📊 {stock_name}({stock_code})")
                                logging.info(f"     💰 현재가: {current_price:,}원 | 보유수량: {quantity:,}주 | 매입단가: {avg_price:,}원")
                                
                                if profit_loss > 0:
                                    logging.info(f"     📈 평가손익: +{profit_loss:,}원 (+{profit_rate:.2f}%)")
                                elif profit_loss < 0:
                                    logging.info(f"     📉 평가손익: {profit_loss:,}원 ({profit_rate:.2f}%)")
                                else:
                                    logging.info(f"     ➡️ 평가손익: 0원 (0.00%)")
                        
                        logging.info("=" * 70)
                        
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
                                    if ' - ' in item_text:
                                        existing_code = item_text.split(' - ')[0]
                                    else:
                                        existing_code = item_text
                                    
                                    if existing_code == stock_code:
                                        monitoring_exists = True
                                        break
                                
                                if not monitoring_exists:
                                    parent.monitoring_manager.add_stock_to_monitoring(stock_code, stock_name)
                                    logging.debug(f"   ✅ 모니터링 추가 (동기): {stock_code} ({stock_name})")
                                
                                # 보유종목 리스트에 추가
                                holding_exists = False
                                for i in range(parent.boughtBox.count()):
                                    item_text = parent.boughtBox.item(i).text()
                                    # 종목코드 추출 (종목명 유무와 관계없이)
                                    if ' - ' in item_text:
                                        existing_code = item_text.split(' - ')[0]
                                    else:
                                        existing_code = item_text
                                    
                                    if existing_code == stock_code:
                                        holding_exists = True
                                        break
                                
                                if not holding_exists:
                                    parent.boughtBox.addItem(stock_code)
                                    logging.debug(f"   ✅ 보유종목 추가: {stock_code} ({stock_name})")
                        
                        logging.info("✅ 보유종목이 모니터링과 보유종목 리스트에 추가되었습니다")
                        logging.info("📡 이후 실시간 변동은 웹소켓으로 업데이트됩니다")
                        
                        # REST API 잔고 데이터를 웹소켓 balance_data에 저장 (중요!)
                        self._initialize_balance_data_from_rest_api(holdings)
                        
                        # 투자현황표 직접 업데이트는 _initialize_balance_data_from_rest_api 내부에서 수행됨
                        
                    else:
                        logging.info("📦 현재 보유 종목이 없습니다.")
                else:
                    logging.warning("⚠️ 계좌 잔고 조회 실패 또는 보유종목 없음")
                    
            except Exception as balance_ex:
                logging.error(f"❌ 계좌 잔고 조회 실패: {balance_ex}")
                logging.error(f"잔고 조회 예외 상세: {traceback.format_exc()}")
                
        except Exception as ex:
            logging.error(f"❌ 계좌 잔고 조회 실패: {ex}")
            logging.error(f"계좌 조회 예외 상세: {traceback.format_exc()}")

    async def handle_acnt_balance_query_async(self):
        """계좌 잔고조회 (비동기 버전) - post_login_setup에서 사용"""
        parent = self.parent
        try:
            logging.debug("🔧 계좌 잔고 조회 시작 (비동기)")
            
            if not hasattr(parent, 'trader') or not parent.trader:
                logging.warning("⚠️ 트레이더가 초기화되지 않았습니다")
                return
            
            # 1. 예수금상세현황 조회
            try:
                deposit_data = parent.trader.client.get_deposit_detail()
                if deposit_data:
                    parent.ui_manager.display_deposit_info(deposit_data)
            except Exception as deposit_ex:
                logging.error(f"❌ 예수금상세현황 조회 실패: {deposit_ex}")

            # 2. REST API 잔고조회
            try:
                balance_data = parent.trader.client.get_acnt_balance()
                if balance_data:
                    holdings = balance_data.get('stk_acnt_evlt_prst', balance_data.get('output1', []))
                    
                    if holdings and len(holdings) > 0:
                        logging.info(f"📦 보유 종목 수: {len(holdings)}개 (비동기 조회)")
                        
                        # 보유종목을 모니터링과 보유종목 리스트에 추가
                        for stock in holdings:
                            raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                            stock_code = parent.data_manager.normalize_stock_code(raw_code)
                            stock_name = stock.get('stk_nm', stock.get('prdt_name', ''))
                            quantity = parent.data_manager.safe_int(stock.get('rmnd_qty', stock.get('hldg_qty', 0)))
                            
                            if stock_code and quantity > 0:
                                # 모니터링 리스트에 비동기로 추가 (실시간 구독 포함)
                                await parent.monitoring_manager.add_stock_to_monitoring_async(stock_code, stock_name)
                                
                                # 보유종목 리스트에 추가 (UI)
                                holding_exists = False
                                for i in range(parent.boughtBox.count()):
                                    item_text = parent.boughtBox.item(i).text()
                                    if stock_code in item_text:
                                        holding_exists = True
                                        break
                                
                                if not holding_exists:
                                    parent.boughtBox.addItem(stock_code)
                                    logging.debug(f"   ✅ 보유종목 UI 추가: {stock_code} ({stock_name})")
                        
                        logging.info("✅ 보유종목이 모니터링과 보유종목 리스트에 추가되었습니다 (비동기)")
                        
                        # REST API 잔고 데이터를 웹소켓 balance_data에 저장
                        self._initialize_balance_data_from_rest_api(holdings)
                        
                    else:
                        logging.info("📦 현재 보유 종목이 없습니다 (비동기 조회).")
                else:
                    logging.warning("⚠️ 계좌 잔고 조회 실패 또는 보유종목 없음 (비동기 조회)")
                    
            except Exception as balance_ex:
                logging.error(f"❌ 계좌 잔고 조회 실패 (비동기): {balance_ex}")
                
        except Exception as ex:
            logging.error(f"❌ 계좌 잔고 조회 실패 (비동기): {ex}")

    
    def _initialize_balance_data_from_rest_api(self, holdings):
        """REST API 잔고 데이터를 웹소켓 balance_data 형식으로 변환하고 투자현황표 업데이트"""
        
        parent = self.parent
        
        try:
            logging.debug("🔧 REST API 잔고 데이터를 웹소켓 balance_data 형식으로 변환 중...")
            
            # 웹소켓 클라이언트 확인
            if not hasattr(parent, 'login_handler') or not parent.login_handler:
                logging.warning("⚠️ login_handler 객체가 없습니다 - 데이터를 임시 저장합니다")
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
            for stock in holdings:
                stock_code = '알수없음'  # 예외 핸들링을 위한 기본값
                try:
                    # REST API 필드명 매핑
                    raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                    stock_code = parent.data_manager.normalize_stock_code(raw_code)  # A 접두사 제거
                    stock_name = stock.get('stk_nm', stock.get('prdt_name', ''))
                    quantity = parent.data_manager.safe_int(stock.get('rmnd_qty', stock.get('hldg_qty', 0)))
                    current_price = parent.data_manager.safe_int(stock.get('cur_prc', stock.get('prpr', 0)))
                    average_price = parent.data_manager.safe_int(stock.get('avg_prc', stock.get('pchs_avg_pric', 0)))
                    profit_loss = parent.data_manager.safe_int(stock.get('pl_amt', stock.get('evlu_pfls_amt', 0)))
                    profit_rate = parent.data_manager.safe_float(stock.get('pl_rt', stock.get('evlu_pfls_rt', 0)))
                    
                    if stock_code and quantity > 0:
                        # 평가금액 계산
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
                            if stock_code not in parent.trader.holdings:
                                parent.trader.holdings[stock_code] = {'quantity': quantity}
                                # 매입 가격 및 시간 설정
                                if stock_code not in parent.trader.buy_prices:
                                    parent.trader.buy_prices[stock_code] = average_price
                                if stock_code not in parent.trader.buy_times:
                                    # REST API에는 매입 시간이 없으므로 현재 시간 사용
                                    parent.trader.buy_times[stock_code] = datetime.now()
                                logging.debug(f"   ✅ {stock_code} trader.holdings에 추가 (초기 로드, 수량: {quantity}주, 매입단가: {average_price}원)")
                            else:
                                # 이미 있는 경우 수량 업데이트
                                parent.trader.holdings[stock_code]['quantity'] = quantity
                                if stock_code not in parent.trader.buy_prices or parent.trader.buy_prices[stock_code] == 0:
                                    parent.trader.buy_prices[stock_code] = average_price
                        
                        converted_count += 1
                        
                        logging.debug(f"   ✅ {stock_code} 변환 완료: {quantity}주, {current_price:,}원")
                        
                except Exception as item_ex:
                    logging.error(f"❌ 종목 데이터 변환 실패 ({stock_code}): {item_ex}")
                    continue
            
            logging.info(f"✅ REST API 잔고 데이터 변환 완료: {converted_count}개 종목")
            
            # 웹소켓 balance_data에 저장 완료
            logging.info(f"✅ 웹소켓 balance_data에 {converted_count}개 종목 저장 완료")
            logging.info(f"   저장된 종목 목록: {list(ws_client.balance_data.keys())}")
            
            # 투자현황표 업데이트 (웹소켓 balance_data 사용)
            if converted_count > 0:
                logging.debug("🔧 투자 현황표 업데이트 시작 (REST API 잔고 → 웹소켓 balance_data)")
                parent.update_stock_table()
            
        except Exception as ex:
            logging.error(f"❌ REST API 잔고 데이터 변환 실패: {ex}")
            logging.error(f"변환 실패 예외 상세: {traceback.format_exc()}")
    

class ConditionSearchManager:
    """조건검색 관리 매니저"""
    
    def __init__(self, parent):
        self.parent = parent

    async def handle_condition_search_list_query(self):
        """조건검색 목록조회 (웹소켓 기반)"""
        try:
            logging.debug("🔍 조건검색 목록조회 시작 (웹소켓)")

            if not hasattr(self.parent, 'trader') or not self.parent.trader:
                logging.warning("⚠️ 트레이더가 초기화되지 않았습니다")
                return

            if not hasattr(self.parent.trader, 'client') or not self.parent.trader.client:
                logging.warning("⚠️ API 클라이언트가 연결되지 않았습니다")
                return

            # 웹소켓 클라이언트 확인
            if not hasattr(self.parent.login_handler, 'websocket_client') or not self.parent.login_handler.websocket_client:
                logging.warning("⚠️ 웹소켓 클라이언트가 연결되지 않았습니다")
                return

            # 웹소켓을 통한 조건검색 목록조회
            try:
                await self.parent.login_handler.websocket_client.send_message({
                    'trnm': 'CNSRLST', # TR명
                })
                logging.debug("✅ 조건검색 목록조회 요청 전송 완료 (웹소켓)")

                # 웹소켓 응답은 receive_messages에서 처리됨
                logging.debug("💾 조건검색 목록조회 요청 완료 - 응답은 웹소켓에서 처리됩니다")

            except Exception as websocket_ex:
                logging.error(f"❌ 조건검색 목록조회 웹소켓 요청 실패: {websocket_ex}")
                self.parent.condition_search_list = None
        except Exception as ex:
            logging.error(f"❌ 조건검색 목록조회 실패: {ex}")
            self.parent.condition_search_list = None
    
    def load_condition_list(self):
        """조건검색식 목록을 투자전략 콤보박스에 추가"""
        try:
            logging.debug("🔍 조건검색식 목록 로드 시작")
            logging.debug("📋 조건검색은 웹소켓을 통해서만 작동합니다")
            
            # 키움 클라이언트 참조 확인           
            if not self.parent.login_handler.kiwoom_client:
                logging.warning("⚠️ 키움 클라이언트가 초기화되지 않았습니다")
                self.parent.update_condition_status("실패")
                return
            
            # 웹소켓 연결 상태 확인
            websocket_connected = False
            if hasattr(self.parent.login_handler, 'websocket_client') and self.parent.login_handler.websocket_client:
                websocket_connected = self.parent.login_handler.websocket_client.connected
                logging.debug(f"🔍 웹소켓 연결 상태: {websocket_connected}")
            
            if not websocket_connected:
                logging.warning("⚠️ 웹소켓이 연결되지 않았습니다.")
                self.parent.update_condition_status("웹소켓 미연결")
                return
            
            logging.debug("✅ 웹소켓 연결 상태 확인 완료")
            logging.debug("🔍 웹소켓을 통한 조건검색식 목록 조회 시작")
            
            # 웹소켓을 통해 받은 조건검색 목록 사용
            if not hasattr(self.parent, 'condition_search_list') or not self.parent.condition_search_list:
                logging.warning("⚠️ 웹소켓을 통해 받은 조건검색 목록이 없습니다")
                self.parent.update_condition_status("목록 없음")
                return
            
            # 웹소켓으로 받은 조건검색 목록을 변환
            condition_list = []
            for condition in self.parent.condition_search_list:
                seq = condition['seq']
                name = condition['title']
                condition_list.append((seq, name))
            
            if condition_list:
                self.parent.condition_list = condition_list
                logging.debug(f"📋 조건검색식 목록 조회 성공: {len(condition_list)}개")
                
                # 투자전략 콤보박스에 조건검색식 추가
                added_count = 0
                for seq, name in condition_list:
                    condition_text = name  # [조건검색] 접두사 제거
                    self.parent.comboStg.addItem(condition_text)
                    added_count += 1
                    logging.debug(f"✅ 조건검색식 추가 ({added_count}/{len(condition_list)}): {condition_text}")
                
                logging.debug(f"✅ 조건검색식 목록 로드 완료: {len(condition_list)}개 종목이 투자전략 콤보박스에 추가됨")
                
                # 조건검색식 로드 후 저장된 조건검색식이 있는지 확인하고 자동 실행
                logging.debug("🔍 저장된 조건검색식 자동 실행 확인 시작")
                self.check_and_auto_execute_saved_condition()
                
            else:
                logging.warning("⚠️ 조건검색식 목록이 비어있습니다")
                logging.debug("📋 키움증권 HTS에서 조건검색식을 먼저 생성하세요")
                self.parent.update_condition_status("목록 없음")
            
        except Exception as ex:
            logging.error(f"❌ 조건검색식 목록 로드 실패: {ex}")
            logging.error(f"조건검색식 목록 로드 에러 상세: {traceback.format_exc()}")
            self.parent.update_condition_status("실패")
    
    def check_and_auto_execute_saved_condition(self):
        """저장된 조건검색식이 있는지 확인하고 자동 실행"""
        try:
            
            # settings.ini에서 저장된 전략 확인
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            if config.has_option('SETTINGS', 'last_strategy'):
                last_strategy = config.get('SETTINGS', 'last_strategy')
                logging.debug(f"📋 저장된 전략 확인: {last_strategy}")
                
                # 저장된 전략이 조건검색식인지 확인 (조건검색 목록에 있는지 확인)
                if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                    condition_names = [condition['title'] for condition in self.parent.condition_search_list]
                    if last_strategy in condition_names:
                        logging.debug(f"🔍 저장된 조건검색식 발견: {last_strategy}")
                        
                        # 콤보박스에서 해당 조건검색식 찾기
                        index = self.parent.comboStg.findText(last_strategy)
                        if index >= 0:
                            # 조건검색식 선택
                            self.parent.comboStg.setCurrentIndex(index)
                            logging.debug(f"✅ 저장된 조건검색식 선택: {last_strategy}")
                            
                            # 자동 실행 (1초 후)
                            async def delayed_condition_search():
                                await asyncio.sleep(1.0)  # 1초 대기
                                await self.parent.handle_condition_search()
                            asyncio.create_task(delayed_condition_search())
                            logging.debug("🔍 저장된 조건검색식 자동 실행 예약 (1초 후)")
                            logging.debug("📋 조건검색식이 자동으로 실행되어 모니터링 종목에 추가됩니다")
                            return True  # 저장된 조건검색식 실행됨
                
                # 통합 전략인 경우 모든 조건검색식 실행
                if last_strategy == "통합 전략":
                    logging.debug(f"🔍 저장된 통합 전략 발견: {last_strategy}")
                    
                    # 콤보박스에서 통합 전략 찾기
                    index = self.parent.comboStg.findText(last_strategy)
                    if index >= 0:
                        # 통합 전략 선택
                        self.parent.comboStg.setCurrentIndex(index)
                        logging.debug(f"✅ 저장된 통합 전략 선택: {last_strategy}")
                        
                        # 자동 실행 (1초 후)
                        async def delayed_integrated_search():
                            try:
                                await asyncio.sleep(1.0)  # 1초 대기
                                await self.parent.handle_integrated_condition_search()
                            except asyncio.CancelledError:
                                # 태스크가 취소되면 조용히 종료
                                logging.debug("통합 조건검색 태스크 취소됨")
                                return
                            except Exception as e:
                                logging.error(f"통합 조건검색 실행 실패: {e}")
                        
                        # 태스크 생성 및 실행 (취소 가능하도록 설정)
                        task = asyncio.create_task(delayed_integrated_search())
                        # 태스크를 저장하여 필요시 취소할 수 있도록 함
                        self.parent._delayed_search_task = task
                        logging.debug("🔍 저장된 통합 전략 자동 실행 예약 (1초 후)")
                        logging.debug("📋 모든 조건검색식이 자동으로 실행되어 모니터링 종목에 추가됩니다")
                        return True  # 저장된 통합 전략 실행됨
                    else:
                        logging.warning(f"⚠️ 저장된 조건검색식을 콤보박스에서 찾을 수 없습니다: {last_strategy}")
                        logging.debug("📋 조건검색식 목록을 다시 확인하거나 수동으로 선택하세요")
                        return False  # 저장된 조건검색식이 콤보박스에 없음
                else:
                    logging.debug(f"📋 저장된 전략이 조건검색식이 아닙니다: {last_strategy}")
                    logging.debug("📋 일반 투자전략이 선택되어 있습니다")
                    return False  # 조건검색식이 아님
            else:
                logging.debug("📋 저장된 전략이 없습니다")
                logging.debug("📋 투자전략 콤보박스에서 원하는 전략을 선택하세요")
                return False  # 저장된 전략이 없음
            
        except Exception as ex:
            logging.error(f"❌ 저장된 조건검색식 확인 및 자동 실행 실패: {ex}")
            logging.error(f"저장된 조건검색식 확인 에러 상세: {traceback.format_exc()}")
            return False  # 오류 발생
    
    async def handle_integrated_condition_search(self):
        """통합 전략 실행: 모든 조건검색식 순차적으로 실행"""
        try:
            if not hasattr(self.parent, 'condition_search_list') or not self.parent.condition_search_list:
                logging.warning("⚠️ 조건검색 목록이 없어 통합 검색을 실행할 수 없습니다.")
                return

            logging.info(f"🔄 통합 조건검색 시작: {len(self.parent.condition_search_list)}개 조건식 실행")

            for condition in self.parent.condition_search_list:
                seq = condition.get('seq')
                name = condition.get('title')
                if seq and name:
                    logging.debug(f"  - 조건검색 실행: {name} (seq: {seq})")
                    # 각 조건검색을 순차적으로 실행하고, API 제한을 피하기 위해 약간의 지연을 둡니다.
                    await self.parent.start_condition_realtime(seq, name)
                    await asyncio.sleep(1) # API 요청 간격

            logging.debug("✅ 모든 조건검색식에 대한 실시간 모니터링이 시작되었습니다.")

        except Exception as ex:
            logging.error(f"❌ 통합 조건검색 실행 실패: {ex}")


# ==================== 메인 윈도우 ====================
class MyWindow(QWidget):
    """메인 윈도우 클래스"""
    
    def __init__(self):
        super().__init__()
        
        # 기본 변수 초기화
        self.is_loading_strategy = False
        self.market_close_emitted = False
        
        # PyQtGraph 기반 차트 위젯 사용 (고정)
        
        # 객체 초기화 (트레이더는 API 연결 후 생성)
        self.objstg = None
        self.autotrader = None
        self.chart_cache = None  # 차트 데이터 캐시
        self.realtime_chart_widget = None  # 실시간 차트 위젯
        
        # 조건검색 관련 변수
        self.condition_list = []  # 조건검색 목록
        self.active_realtime_conditions = set()  # 활성화된 실시간 조건검색
        self.condition_search_results = {}  # 조건검색 결과 저장
        self.stock_condition_map = {}  # 종목별 조건검색 이름 매핑 (종목코드: 조건검색 이름)
        self.current_condition_name = None  # 현재 실행 중인 조건검색 이름 (응답 처리용)
        self.chart_drawing_lock = Lock()
        
        # Manager 초기화 (UI 생성 전에 초기화)
        self.data_manager = DataManager(self)
        self.monitoring_manager = MonitoringManager(self)
        self.ui_manager = UIComponentsManager(self)
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
    
    # 차트 위젯 설정 로드 메서드 제거됨 - PyQtGraph 고정 사용

    def apply_modern_style(self):
        """현대적이고 눈에 피로하지 않은 스타일 적용 (UIComponentsManager로 위임)"""
        self.ui_manager.apply_modern_style()
    
    def init_ui(self):
        """UI 초기화 (UIComponentsManager로 위임)"""
        self.ui_manager.init_ui()
        
        # 창 표시 안정성을 위한 추가 설정
        self.show()  # 창을 먼저 표시
        self.raise_()  # 창을 맨 앞으로
        self.activateWindow()  # 창 활성화
    
    def init_trading_tab(self):
        """실시간 매매 탭 초기화 (UIComponentsManager로 위임)"""
        self.ui_manager.init_trading_tab()
    
    def init_backtest_tab(self):
        """백테스팅 탭 초기화 (BacktestManager로 위임)"""
        self.backtest_manager.init_backtest_tab()
    
    async def attempt_auto_connect(self):
        """자동 연결 시도"""
        try:
            # 자동 연결이 활성화되어 있고, 아직 post_login_setup이 실행되지 않았다면
            if self.login_handler.config.getboolean('LOGIN', 'autoconnect', fallback=False):
                if self._post_login_setup_done:
                    logging.debug("이미 자동 연결 및 초기화가 완료되었습니다.")
                    return
                self.login_handler.handle_api_connection()
                await self.login_handler.start_websocket_client()
                
        except Exception as ex:
            logging.error(f"자동 연결 시도 실패: {ex}")    

    def subscribe_holdings_realtime(self, holding_codes):
        """보유종목에 대한 실시간 구독 실행 (중단됨)"""
        try:
            # 실시간 구독 요청 중단
            logging.debug(f"⏸️ 보유종목 실시간 구독 중단: {holding_codes}")
            
            # 웹소켓 구독 기능 비활성화 (중복 구독 방지)
            # if hasattr(self, 'trader') and self.trader and self.trader.client:
            #     if hasattr(self.trader.client, 'ws_client') and self.trader.client.ws_client:
            #         # 중복 구독 방지: 기존 보유 종목 구독과 비교
            #         existing_holdings = getattr(self.trader.client.ws_client, 'holdings_subscribed', set())
            #         new_holdings = set(holding_codes)
            #         
            #         if existing_holdings != new_holdings:
            #             # 보유종목에 대한 실시간 구독
            #             self.trader.client.ws_client.add_subscription(holding_codes, 'holdings')
            #             self.trader.client.ws_client.holdings_subscribed = new_holdings
            #             logging.debug(f"🔄 보유종목 실시간 구독 실행: {holding_codes}")
            #         else:
            #             logging.debug(f"보유종목 구독 변경 없음, 업데이트 건너뜀: {holding_codes}")
        except Exception as ex:
            logging.error(f"❌ 보유종목 실시간 구독 실패: {ex}")
            logging.error(f"보유종목 구독 예외 상세: {traceback.format_exc()}")
            logging.debug("⚠️ 보유종목 구독 실패했지만 프로그램을 계속 실행합니다")
    
    def extract_monitoring_stock_codes(self):
        """모니터링 종목 코드 추출 (MonitoringManager로 위임)"""
        return self.monitoring_manager.extract_monitoring_stock_codes_enhanced()
    
    
    
    def add_balance_stock_to_holdings(self, balance_info):
        """실시간 잔고 데이터를 받아 보유 종목에 자동 추가 (UIComponentsManager로 위임)"""
        self.ui_manager.add_balance_stock_to_holdings(balance_info)
    
    
    def _display_deposit_info(self, deposit_data):
        """예수금상세현황 정보 표시 (UIComponentsManager로 위임)"""
        self.ui_manager.display_deposit_info(deposit_data)
    
    def trading_mode_changed(self):
        """거래 모드 변경 (TradingManager로 위임)"""
        self.trading_manager.trading_mode_changed()
    
    def load_strategy_combos(self):
        """전략 콤보박스에 settings.ini 값 로드 (StrategyManager로 위임)"""
        self.strategy_manager.load_strategy_combos()
    
    def _load_strategy_list(self, combo_widget, key_prefix, strategy_type):
        """전략 목록 로드 (StrategyManager로 위임)"""
        self.strategy_manager._load_strategy_list(combo_widget, key_prefix, strategy_type)
    
    def load_buy_strategies(self):
        """매수전략 콤보박스 로드 (StrategyManager로 위임)"""
        self.strategy_manager.load_buy_strategies()
    
    def load_sell_strategies(self):
        """매도전략 콤보박스 로드 (StrategyManager로 위임)"""
        self.strategy_manager.load_sell_strategies()
    
    def load_initial_strategy_content(self):
        """초기 전략 내용을 텍스트박스에 로드 (StrategyManager로 위임)"""
        self.strategy_manager.load_initial_strategy_content()
    
    def add_stock_to_list(self):
        """투자 대상 종목 리스트에 종목 추가 (TradingManager로 위임)"""
        self.trading_manager.add_stock_to_list()
    
    def normalize_stock_input(self, stock_input):
        """종목 입력값을 정규화하여 종목코드와 종목명 반환 (DataManager로 위임)"""
        return self.data_manager.normalize_stock_input(stock_input)
    
    def get_stock_name_by_code(self, stock_code):
        """종목코드로 종목명 조회 (DataManager로 위임)"""
        return self.data_manager.get_stock_name_by_code(stock_code)
    
    def get_stock_code_by_name(self, stock_name):
        """종목명으로 종목코드 조회 (DataManager로 위임)"""
        return self.data_manager.get_stock_code_by_name(stock_name)
    
    def get_monitoring_stock_codes(self):
        """모니터링 박스에서 종목 코드 리스트 추출 (MonitoringManager로 위임)"""
        return self.monitoring_manager.get_monitoring_stock_codes()
    
    
    def buycount_setting(self):
        """투자 종목수 설정 (TradingManager로 위임)"""
        self.trading_manager.buycount_setting()
    
    def buy_item(self):
        """종목 매입 (TradingManager로 위임)"""
        self.trading_manager.buy_item()
    
    def delete_select_item(self):
        """선택된 종목 삭제 (TradingManager로 위임)"""
        self.trading_manager.delete_select_item()
    
    def sell_item(self):
        """종목 매도 (TradingManager로 위임)"""
        self.trading_manager.sell_item()
    
    def sell_all_item(self):
        """전체 매도 (TradingManager로 위임)"""
        self.trading_manager.sell_all_item()
    
    

    def listBoxChanged(self, current):
        """리스트박스 클릭 이벤트 - 차트 표시 (UIComponentsManager로 위임)"""
        self.ui_manager.listBoxChanged(current)          
    
    def on_chart_data_updated(self, code):
        """차트 데이터 업데이트 시그널 핸들러 (UIComponentsManager로 위임)"""
        self.ui_manager.on_chart_data_updated(code)
    
    def on_chart_data_updated_for_trading(self, code):
        """차트 데이터 업데이트 시 AutoTrader에 매매 판단 위임 (TradingManager로 위임)"""
        self.trading_manager.on_chart_data_updated_for_trading(code)

    def stgChanged(self):
        """전략 변경 (StrategyManager로 위임)"""
        self.strategy_manager.stg_changed()
    
    def buyStgChanged(self):
        """매수 전략 변경 (StrategyManager로 위임)"""
        self.strategy_manager.buy_stg_changed()
    
    def sellStgChanged(self):
        """매도 전략 변경 (StrategyManager로 위임)"""
        self.strategy_manager.sell_stg_changed()
    
    def load_strategy_content(self, strategy_name, strategy_type):
        """전략 내용을 텍스트 위젯에 로드 (StrategyManager로 위임)"""
        self.strategy_manager.load_strategy_content(strategy_name, strategy_type)
    
    def load_backtest_strategies(self):
        """백테스팅 전략 콤보박스 로드 (BacktestManager로 위임)"""
        self.backtest_manager.load_backtest_strategies()
    
    def _save_strategy(self, text_widget, combo_widget, key_prefix, strategy_type):
        """전략 저장 (StrategyManager로 위임)"""
        self.strategy_manager._save_strategy(text_widget, combo_widget, key_prefix, strategy_type)
    
    def save_buystrategy(self):
        """매수 전략 저장 (StrategyManager로 위임)"""
        self.strategy_manager.save_buystrategy()
    
    def save_sellstrategy(self):
        """매도 전략 저장 (StrategyManager로 위임)"""
        self.strategy_manager.save_sellstrategy()
    
    def load_db_period(self):
        """DB 기간 불러오기 (BacktestManager로 위임)"""
        self.backtest_manager.load_db_period()
    
    def run_backtest(self):
        """백테스팅 실행 (BacktestManager로 위임)"""
        self.backtest_manager.run_backtest()
   
    async def post_login_setup(self):
        """로그인 후 설정"""
        try:
            # 중복 실행 방지
            if self._post_login_setup_done:
                logging.debug("post_login_setup이 이미 실행되었습니다. 건너뜁니다.")
                return

            # 로거 설정: UI 로그창에는 INFO 레벨까지만 표시 (터미널은 DEBUG까지 표시)
            logger = logging.getLogger()
            if not any(isinstance(handler, QTextEditLogger) for handler in logger.handlers):
                text_edit_logger = QTextEditLogger(self.terminalOutput)
                text_edit_logger.setLevel(logging.INFO)  # UI 창은 INFO 이상만 표시
                logger.addHandler(text_edit_logger)

            # 1. 트레이더 객체 확인 (이미 API 연결 시 생성됨)
            if not hasattr(self, 'trader') or not self.trader:
                logging.warning("⚠️ 트레이더 객체가 없습니다. API 연결을 확인해주세요.")
                return

            # 2. 전략 객체 초기화
            if not self.objstg:
                self.objstg = KiwoomStrategy(self.trader, self)
                logging.debug("🔍 KiwoomStrategy 객체 생성 완료")

            # 3. 시그널 연결
            try:
                if self.trader:
                    logging.debug("🔍 트레이더 시그널 연결 중...")
                    self.trader.signal_update_balance.connect(self.update_acnt_balance_display)
                    self.trader.signal_order_result.connect(self.update_order_result)
                    logging.debug("✅ 트레이더 시그널 연결 완료")
                else:
                    logging.warning("⚠️ 트레이더 객체가 없어 시그널 연결을 건너뜁니다")
                if self.objstg:
                    logging.debug("🔍 전략 시그널 연결 중...")
                    self.objstg.signal_strategy_result.connect(self.update_strategy_result)
                    logging.debug("✅ 전략 시그널 연결 완료")
                else:
                    logging.warning("⚠️ 전략 객체가 없어 시그널 연결을 건너뜁니다")
            except Exception as signal_ex:
                logging.error(f"❌ 시그널 연결 실패: {signal_ex}")
                logging.error(f"시그널 연결 예외 상세: {traceback.format_exc()}")

            # 4. 차트 데이터 캐시 초기화
            try:
                if not self.chart_cache:
                    self.chart_cache = ChartDataCache(self.trader, self)
                    logging.debug("🔍 ChartDataCache 객체 생성 완료")
                    
                    # 실시간 차트 위젯과 데이터 캐시 연결
                    if hasattr(self, 'realtime_chart_widget') and self.realtime_chart_widget:
                        self.chart_cache.data_updated.connect(self.on_chart_data_updated)
                        logging.debug("🔍 실시간 차트 위젯과 데이터 캐시 연결 완료")
                if hasattr(self.login_handler, 'kiwoom_client') and self.login_handler.kiwoom_client:
                    self.login_handler.kiwoom_client.chart_cache = self.chart_cache
                    logging.debug("🔍 chart_cache를 KiwoomRestClient에 설정 완료")
                logging.debug("✅ 차트 데이터 캐시 초기화 완료")
            except Exception as cache_ex:
                logging.error(f"❌ 차트 데이터 캐시 초기화 실패: {cache_ex}")
                logging.error(f"차트 캐시 초기화 예외 상세: {traceback.format_exc()}")
                self.chart_cache = None

            # 5. 자동매매 객체 초기화 및 시작
            if not self.autotrader:
                self.autotrader = AutoTrader(self.trader, self)
                logging.debug("🔍 AutoTrader 객체 생성 완료")
                
                # AutoTrader 자동 시작 (evaluation_interval 주기로 매매 판단)
                self.autotrader.start_auto_trading()
                logging.debug(f"✅ 자동매매 시작 완료 ({self.trader.evaluation_interval}초 주기)")

            # 6. 조건검색 목록조회 (웹소켓)
            try:
                # 웹소켓 클라이언트가 연결되어 있는지 확인
                if hasattr(self.login_handler, 'websocket_client') and self.login_handler.websocket_client:
                    if self.login_handler.websocket_client.connected: # 조건검색 목록조회
                        # 웹소켓을 통한 조건검색 목록조회
                        await self.handle_condition_search_list_query()
                        logging.debug("✅ 조건검색 목록조회 완료 (웹소켓)")
                    else:
                        logging.warning("⚠️ 웹소켓이 연결되지 않아 조건검색 목록조회를 건너뜁니다")
                        logging.debug(f"🔍 웹소켓 연결 상태: connected={self.login_handler.websocket_client.connected}")
                else:
                    logging.warning("⚠️ 웹소켓 클라이언트가 없어 조건검색 목록조회를 건너뜁니다")
                    logging.debug(f"🔍 login_handler.websocket_client 존재: {hasattr(self.login_handler, 'websocket_client')}")
                    if hasattr(self.login_handler, 'websocket_client'):
                        logging.debug(f"🔍 websocket_client 값: {self.login_handler.websocket_client}")
            except Exception as condition_ex:
                logging.error(f"❌ 조건검색 목록조회 실패: {condition_ex}")
                logging.error(f"조건검색 목록조회 예외 상세: {traceback.format_exc()}")

            # 7. 계좌 잔고조회 (즉시 실행)
            try:
                await self.account_manager.handle_acnt_balance_query_async()
                logging.debug("✅ 계좌 잔고조회 즉시 실행 완료 (비동기)")
            except Exception as balance_ex:
                logging.error(f"❌ 계좌 잔고조회 실행 실패: {balance_ex}")
                logging.error(f"잔고조회 실행 예외 상세: {traceback.format_exc()}")
            
            # 8. 백테스팅 탭의 DB 기간 로드
            self.load_db_period()
            logging.debug("✅ 백테스팅 탭 DB 기간 로드 완료")

            # 8. 대기 중인 API 큐 처리 (트레이더 객체 생성 후)
            try:
                if hasattr(self, 'chart_cache') and self.chart_cache:
                    if hasattr(self.chart_cache, 'api_request_queue'):
                        queue_size = len(self.chart_cache.api_request_queue)
                        if queue_size > 0:
                            logging.debug(f"🔧 대기 중인 API 큐 처리 시작: {queue_size}개 종목")
                            # 큐 처리 타이머 시작 (3초 간격으로 자동 처리)
                            self.chart_cache._start_queue_processing()
                            logging.debug("✅ 대기 중인 API 큐 처리 타이머 시작")
                        else:
                            logging.debug("🔍 대기 중인 API 큐가 없습니다")
                    else:
                        logging.debug("🔍 API 큐가 초기화되지 않았습니다")
                else:
                    logging.debug("🔍 차트 캐시가 초기화되지 않았습니다")
            except Exception as queue_ex:
                logging.error(f"❌ API 큐 처리 실패: {queue_ex}")
                logging.error(f"API 큐 처리 예외 상세: {traceback.format_exc()}")

        except Exception as ex:
            logging.error(f"❌ 로그인 후 초기화 실패: {ex}")
            logging.error(f"초기화 실패 예외 상세: {traceback.format_exc()}")
            logging.debug("⚠️ 초기화 실패했지만 프로그램을 계속 실행합니다")
        finally:
            # 실행 완료 플래그 설정
            self._post_login_setup_done = True
    
    # --- UI 업데이트 및 이벤트 핸들러 (각 Manager에 위임) ---

    def update_connection_ui(self, is_connected):
        """연결 상태 UI 업데이트 (UIComponentsManager 위임)"""
        self.ui_manager.update_connection_ui(is_connected)

    def update_acnt_balance_display(self, balance_data):
        """잔고 정보 UI 업데이트 (UIComponentsManager 위임)"""
        self.ui_manager.update_acnt_balance_display(balance_data)

    def update_stock_table(self):
        """투자 현황표 UI 업데이트 (UIComponentsManager 위임)"""
        self.ui_manager.update_stock_table()

    def update_order_result(self, code, order_type, quantity, price, success):
        """주문 결과 UI 업데이트 (UIComponentsManager 위임)"""
        self.ui_manager.update_order_result(code, order_type, quantity, price, success)

    def update_strategy_result(self, code, action, data):
        """전략 결과 UI 업데이트 (UIComponentsManager 위임)"""
        self.ui_manager.update_strategy_result(code, action, data)

    def on_chart_data_updated(self, code):
        """차트 데이터 업데이트 시그널 핸들러 (UIComponentsManager 위임)"""
        self.ui_manager.on_chart_data_updated(code)

    def on_chart_data_updated_for_trading(self, code):
        """차트 데이터 업데이트 시 매매 판단 위임 (TradingManager 위임)"""
        self.trading_manager.on_chart_data_updated_for_trading(code)

    def listBoxChanged(self, current):
        """리스트박스 클릭 이벤트 (UIComponentsManager 위임)"""
        self.ui_manager.listBoxChanged(current)

    # --- 조건검색 관련 메서드 (ConditionSearchManager 위임) ---

    async def handle_condition_search_list_query(self):
        """조건검색 목록 조회 (ConditionSearchManager 위임)"""
        await self.condition_search_manager.handle_condition_search_list_query()

    async def handle_integrated_condition_search(self):
        """통합 조건검색 실행 (ConditionSearchManager 위임)"""
        if hasattr(self, 'condition_search_manager'):
            await self.condition_search_manager.handle_integrated_condition_search()

    def closeEvent(self, event):
        """윈도우 종료 이벤트"""
        try:
            # 현재 선택된 투자전략을 settings.ini에 저장 (StrategyManager 위임)
            self.strategy_manager.save_current_strategy()
            
            # 지연된 태스크 취소
            if hasattr(self, '_delayed_search_task') and self._delayed_search_task:
                if not self._delayed_search_task.done():
                    self._delayed_search_task.cancel()
                    logging.debug("✅ 지연된 통합 조건검색 태스크 취소됨")
            
            # 자동매매 중지
            if self.autotrader:
                self.autotrader.stop_auto_trading()
            
            # 차트 관련 정리
            if hasattr(self, 'chartdrawer') and self.chartdrawer:
                try:
                    logging.debug("📊 ChartDrawer 정리 시작")
                    # ChartDrawer의 처리 상태 초기화
                    if hasattr(self.chartdrawer, '_processing_code'):
                        self.chartdrawer._processing_code = None
                    
                    # ChartDrawer 처리 상태 정리
                    if hasattr(self.chartdrawer, '_is_processing'):
                        self.chartdrawer._is_processing = False
                        self.chartdrawer._processing_code = None
                    
                    # ChartDrawer 참조 제거
                    self.chartdrawer = None
                    logging.debug("✅ ChartDrawer 정리 완료")
                except Exception as drawer_ex:
                    logging.error(f"❌ ChartDrawer 정리 실패: {drawer_ex}")
            
            # 차트 데이터 캐시 정리
            if hasattr(self, 'chart_cache') and self.chart_cache:
                try:
                    logging.debug("📊 차트 데이터 캐시 정리 시작")
                    self.chart_cache.stop()
                    logging.debug("✅ 차트 데이터 캐시 정리 완료")
                except Exception as cache_ex:
                    logging.error(f"❌ 차트 데이터 캐시 정리 실패: {cache_ex}")
            
            
            # 웹소켓 클라이언트 종료
            if hasattr(self, 'login_handler') and self.login_handler:
                try:
                    logging.debug("🔌 웹소켓 클라이언트 종료 시작")
                    if hasattr(self.login_handler, 'websocket_client') and self.login_handler.websocket_client:
                        # 웹소켓 연결 종료
                        self.login_handler.websocket_client.keep_running = False
                        self.login_handler.websocket_client.connected = False
                        
                        # 웹소켓 태스크 취소
                        if hasattr(self.login_handler, 'websocket_task') and self.login_handler.websocket_task:
                            self.login_handler.websocket_task.cancel()
                            logging.debug("✅ 웹소켓 태스크 취소 완료")
                        
                        # 웹소켓 연결 강제 종료
                        try:
                            asyncio.get_running_loop()  # 루프 확인
                            # 비동기 disconnect 호출
                            asyncio.create_task(self.login_handler.websocket_client.disconnect())
                            logging.debug("✅ 웹소켓 비동기 연결 해제 완료")
                        except RuntimeError:
                            # 이벤트 루프가 없으면 이미 종료된 것
                            pass
                        except Exception as async_ex:
                            logging.warning(f"⚠️ 웹소켓 비동기 연결 해제 실패: {async_ex}")
                    
                    logging.debug("✅ 웹소켓 클라이언트 종료 완료")
                except Exception as ws_ex:
                    logging.error(f"❌ 웹소켓 클라이언트 종료 실패: {ws_ex}")
                    logging.error(f"웹소켓 종료 에러 상세: {traceback.format_exc()}")
            
            # 키움 클라이언트 연결 해제
            if self.trader and self.trader.client:
                try:
                    logging.debug("🔌 키움 클라이언트 연결 해제 시작")
                    self.trader.client.disconnect()
                    logging.debug("✅ 키움 클라이언트 연결 해제 완료")
                except Exception as disconnect_ex:
                    logging.error(f"❌ 키움 클라이언트 연결 해제 실패: {disconnect_ex}")
                    logging.error(f"연결 해제 에러 상세: {traceback.format_exc()}")
            
            # QTextEdit 관련 객체 정리
            if hasattr(self, 'terminalOutput') and self.terminalOutput:
                try:
                    # 로그 핸들러에서 QTextEdit 참조 제거 (먼저 실행)
                    logger = logging.getLogger()
                    handlers_to_remove = []
                    for handler in logger.handlers:
                        if isinstance(handler, QTextEditLogger):
                            handlers_to_remove.append(handler)
                    
                    for handler in handlers_to_remove:
                        try:
                            # 핸들러의 text_widget 참조를 None으로 설정
                            handler.text_widget = None
                            logger.removeHandler(handler)
                            handler.close()
                        except Exception:
                            # 핸들러 제거 실패 시 무시
                            pass                    
                except Exception as e:
                    # QTextEdit 정리 실패 시 무시 (프로그램 종료 중이므로)
                    pass
            
            # 모든 타이머 정리
            try:
                # 모든 활성 타이머 정리
                for timer in self.findChildren(QTimer):
                    if timer.isActive():
                        timer.stop()
                logging.debug("✅ 모든 타이머 정리 완료")
            except Exception as timer_ex:
                logging.error(f"❌ 타이머 정리 실패: {timer_ex}")
            
            # asyncio 이벤트 루프 정리
            try:
                loop = asyncio.get_running_loop()
                # 모든 태스크 취소
                tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
                if tasks:
                    for task in tasks:
                        task.cancel()
                    logging.debug(f"✅ {len(tasks)}개 asyncio 태스크 취소 완료")
                else:
                    logging.debug("✅ 취소할 asyncio 태스크 없음")
            except RuntimeError:
                # 이벤트 루프가 없으면 이미 정리된 것
                logging.debug("✅ asyncio 이벤트 루프가 이미 정리됨")
            except Exception as asyncio_ex:
                logging.error(f"❌ asyncio 정리 실패: {asyncio_ex}")
            
            # Qt 애플리케이션 정리
            try:
                
                # 모든 위젯 정리
                app = QApplication.instance()
                if app:
                    # 모든 위젯의 부모-자식 관계 정리
                    for widget in app.allWidgets():
                        if widget.parent() is None:  # 최상위 위젯만
                            widget.close()
                            widget.deleteLater()
                    
                    # 이벤트 처리 완료 대기
                    QCoreApplication.processEvents()
                    logging.debug("✅ Qt 위젯 정리 완료")
            except Exception as qt_ex:
                logging.error(f"❌ Qt 정리 실패: {qt_ex}")
            
            # 가비지 컬렉션 실행
            gc.collect()
            
            logging.debug("✅ 프로그램 종료 처리 완료")
            event.accept()
            
        except Exception as ex:
            logging.error(f"윈도우 종료 처리 실패: {ex}")
            event.accept()
    
    async def start_condition_realtime(self, seq, condition_name=None):
        """조건검색 실시간 요청으로 지속적 모니터링 시작 (웹소켓 기반)"""
        try:
            # 웹소켓 클라이언트 확인
            if not hasattr(self.login_handler, 'websocket_client') or not self.login_handler.websocket_client:
                logging.error("❌ 웹소켓 클라이언트가 연결되지 않았습니다")
                return
            
            if not self.login_handler.websocket_client.connected:
                logging.error("❌ 웹소켓이 연결되지 않았습니다")
                return
            
            # 현재 조건검색 이름 저장 (응답 처리 시 사용)
            if condition_name:
                self.current_condition_name = condition_name
                logging.debug(f"🔍 조건검색 실시간 요청 시작 (웹소켓): {seq} ({condition_name})")
            else:
                logging.debug(f"🔍 조건검색 실시간 요청 시작 (웹소켓): {seq}")
            
            # 웹소켓을 통한 조건검색 실시간 요청 (예시코드 방식)
            await self.login_handler.websocket_client.send_message({
                'trnm': 'CNSRREQ',  # 조건검색 실시간 요청 TR명 (예시코드 방식)
                'seq': seq,
                'search_type': '1',  # 조회타입 (실시간)
                'stex_tp': 'K'  # 거래소구분
            })
            
            if condition_name:
                logging.debug(f"✅ 조건검색 실시간 요청 전송 완료 (웹소켓): {seq} ({condition_name})")
            else:
                logging.debug(f"✅ 조건검색 실시간 요청 전송 완료 (웹소켓): {seq}")
            # 응답은 웹소켓에서 처리됨
            logging.debug(f"💾 조건검색 실시간 요청 완료 - 응답은 웹소켓에서 처리됩니다: {seq}")
                
        except Exception as ex:
            logging.error(f"❌ 조건검색 실시간 요청 실패: {ex}")
            logging.error(f"조건검색 실시간 요청 에러 상세: {traceback.format_exc()}")
            self.update_condition_status("실패")

    async def stop_condition_realtime(self, seq):
        """조건검색 실시간 해제 (웹소켓 기반)"""
        try:
            # 웹소켓 클라이언트 확인
            if not hasattr(self.login_handler, 'websocket_client') or not self.login_handler.websocket_client:
                logging.error("❌ 웹소켓 클라이언트가 연결되지 않았습니다")
                return
            
            if not self.login_handler.websocket_client.connected:
                logging.error("❌ 웹소켓이 연결되지 않았습니다")
                return
            
            logging.debug(f"🔍 조건검색 실시간 해제 (웹소켓): {seq}")
            
            # 웹소켓을 통한 조건검색 실시간 해제
            await self.login_handler.websocket_client.send_message({
                'trnm': 'CNSCLR',  # 조건검색 실시간 해제 TR명
                'seq': seq
            })
            
            logging.debug(f"✅ 조건검색 실시간 해제 전송 완료 (웹소켓): {seq}")
            # 응답은 웹소켓에서 처리됨
            logging.debug(f"💾 조건검색 실시간 해제 완료 - 응답은 웹소켓에서 처리됩니다: {seq}")
                
        except Exception as ex:
            logging.error(f"❌ 조건검색 실시간 해제 실패: {ex}")
            logging.error(f"조건검색 실시간 해제 에러 상세: {traceback.format_exc()}")


# ==================== 메인 실행 ====================
async def main():
    """메인 실행 함수 - qasync를 사용한 비동기 처리"""
    print("프로그램 시작")
    setup_logging()
    logging.debug("🚀 프로그램 시작 - 로깅 설정 완료")

    app = qasync.QApplication(sys.argv)
    if not app:
        app = QApplication.instance()

    window = MyWindow()
    window.show()

    # qasync가 관리하는 이벤트 루프가 종료될 때까지 대기
    loop = asyncio.get_running_loop()
    await loop.create_future()

# ==================== PyQtGraph CandlesticItem 클래스 ====================
class CandlesticItem(pg.GraphicsObject):
    """PyQtGraph용 캔들스틱 아이템"""
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

# ==================== PyQtGraph 차트 위젯 클래스 ====================
class PyQtGraphWidget(pg.PlotWidget):
    """PyQtGraph 기반 차트 위젯"""
    def __init__(self, parent=None, title="실시간 차트"):
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
                logging.warning("🔍 PyQtGraphWidget add_candlestic_data: 빈 데이터")
                return
                
            logging.debug(f"🔍 PyQtGraphWidget add_candlestic_data 호출됨 - 데이터 수: {len(data)}")
            
            # 데이터 형식 검사
            if not isinstance(data, (list, tuple)):
                logging.error(f"🔍 PyQtGraphWidget add_candlestic_data: 잘못된 데이터 형식 - {type(data)}")
                return
                
            # 첫 번째 데이터 항목 검사
            if len(data) > 0:
                first_item = data[0]
                if not isinstance(first_item, (list, tuple)) or len(first_item) < 5:
                    logging.error(f"🔍 PyQtGraphWidget add_candlestic_data: 잘못된 데이터 구조 - {first_item}")
                    return
                    
            # 기존 캔들 아이템 제거
            if self.candle_item is not None:
                self.removeItem(self.candle_item)
            
            # 데이터 변환 (timestamp, open, high, low, close)
            data_list = []
            for i, item in enumerate(data):
                try:
                    if not isinstance(item, (list, tuple)) or len(item) < 5:
                        logging.error(f"🔍 PyQtGraphWidget 잘못된 데이터 항목 {i}: {item}")
                        continue
                        
                    timestamp, open_price, high_price, low_price, close_price = item
                    
                    # 가격 데이터 검사
                    try:
                        open_price = float(open_price)
                        high_price = float(high_price)
                        low_price = float(low_price)
                        close_price = float(close_price)
                    except (ValueError, TypeError) as price_error:
                        logging.error(f"🔍 PyQtGraphWidget 가격 데이터 변환 오류 {i}: {price_error}")
                        continue
                    
                    # 인덱스를 타임스탬프로 사용
                    data_list.append((i, open_price, high_price, low_price, close_price))
                    
                    # 첫 번째와 마지막 데이터 디버깅
                    if i == 0:
                        logging.debug(f"🔍 PyQtGraphWidget 첫 번째 캔들: O={open_price}, H={high_price}, L={low_price}, C={close_price}")
                    elif i == len(data) - 1:
                        logging.debug(f"🔍 PyQtGraphWidget 마지막 캔들: O={open_price}, H={high_price}, L={low_price}, C={close_price}")
                        
                except Exception as item_error:
                    logging.error(f"🔍 PyQtGraphWidget 데이터 항목 처리 오류 {i}: {item_error}")
                    continue

            if len(data_list) == 0:
                logging.warning("🔍 PyQtGraphWidget 처리 가능한 데이터가 없습니다")
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
                    
                logging.debug(f"🔍 PyQtGraphWidget 가격 범위: 최저={min_price:.2f}, 최고={max_price:.2f}, 범위={price_range:.2f}")
                logging.debug(f"🔍 PyQtGraphWidget 범례 공간 확보: 상단 여백={top_margin:.2f} (기본 {margin:.2f} + 범례 {legend_space:.2f})")
                self.setYRange(min_price - margin, max_price + top_margin)
                
                # X축 레이블 수동 설정 (test.py의 setup_index_axis_chart 방식 참고)
                self._setup_x_axis_labels(data, chart_type=chart_type)
                
                logging.debug(f"✅ PyQtGraphWidget 캔들 데이터 추가 완료: {len(data_list)}개")
            
        except Exception as ex:
            logging.error(f"❌ 캔들스틱 데이터 추가 실패: {ex}")
            logging.error(f"❌ 캔들스틱 데이터 추가 오류 상세: {traceback.format_exc()}")
    
    
    def add_line_data(self, data, name="Line", color=None):
        """선 차트 데이터 추가"""
        try:
            logging.debug(f"🔍 PyQtGraphWidget add_line_data 호출됨 - 이름: {name}, 데이터 수: {len(data)}")
            
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
            
            logging.debug(f"✅ PyQtGraphWidget 선 차트 데이터 추가 완료")
            
        except Exception as ex:
            logging.error(f"❌ 선 차트 데이터 추가 실패: {ex}")
            logging.error(f"❌ 선 차트 데이터 추가 오류 상세: {traceback.format_exc()}")
    
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
    
    def isVisible(self):
        """가시성 확인"""
        return super().isVisible()
    
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
            logging.debug(f"🔍 add_moving_averages 호출됨 - data: {type(data)}, ma_data: {type(ma_data)}")
            logging.debug(f"🔍 ma_data 키: {list(ma_data.keys()) if isinstance(ma_data, dict) else 'Not dict'}")
            
            if not data or not ma_data:
                logging.warning(f"⚠️ 데이터가 없습니다 - data: {bool(data)}, ma_data: {bool(ma_data)}")
                return
            
            # 기존 이동평균선 제거
            self.clear_moving_averages()
            
            # 차트 유형별 이동평균선 색상 정의
            if chart_type == "tic":
                # 30틱 차트: MA5, MA20, MA60, MA120
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
            logging.debug(f"📊 {chart_type} 차트 이동평균선 범례: {legend_text}")
            
            # 각 이동평균선 그리기
            for ma_type, ma_values in ma_data.items():
                # numpy 배열인 경우 길이 확인 방법 수정
                if hasattr(ma_values, '__len__'):
                    ma_length = len(ma_values)
                else:
                    ma_length = 0
                logging.debug(f"🔍 {ma_type} 처리 중 - 값 개수: {ma_length}")
                
                if ma_type in ma_colors and ma_values is not None and len(ma_values) > 0:
                    # 유효한 데이터만 필터링
                    valid_data = []
                    for i, value in enumerate(ma_values):
                        if value is not None and not (isinstance(value, float) and (value != value or value == 0)):
                            valid_data.append((i, float(value)))
                    
                    logging.debug(f"🔍 {ma_type} 유효한 데이터 개수: {len(valid_data)}")
                    
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
                        
                        logging.debug(f"✅ {ma_type} 이동평균선 추가: {len(valid_data)}개 데이터")
                    else:
                        logging.warning(f"⚠️ {ma_type} 유효한 데이터가 없습니다")
                else:
                    # numpy 배열인 경우 안전한 진리값 확인
                    has_values = ma_values is not None and len(ma_values) > 0
                    logging.warning(f"⚠️ {ma_type} 처리 건너뜀 - 색상: {ma_type in ma_colors}, 값: {has_values}, 길이: {len(ma_values) if hasattr(ma_values, '__len__') else 0}")
            
            # 범례 추가
            self.add_legend()
            
        except Exception as ex:
            logging.error(f"❌ 이동평균선 추가 실패: {ex}")
            logging.error(f"❌ 상세 오류: {traceback.format_exc()}")
    
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
            logging.error(f"❌ 이동평균선 제거 실패: {ex}")
    
    def add_legend(self):
        """범례 추가"""
        try:
            # 기존 범례 제거
            self.clear_legend()
            
            if not self.ma_lines:
                logging.warning("⚠️ 표시할 이동평균선이 없습니다")
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
                
                # 범례 내부 간격 조정 (PyQtGraph 메서드 사용)
                try:
                    self.legend_item.setLabelTextSize('8pt')  # 라벨 텍스트 크기
                    self.legend_item.setLabelSpacing(2)      # 라벨 간격 줄이기
                except AttributeError:
                    # PyQtGraph 버전에 따라 메서드가 없을 수 있음
                    logging.debug("범례 라벨 간격 설정 메서드를 사용할 수 없습니다")
                
                # 각 이동평균선을 범례에 추가
                for item in legend_items:
                    self.legend_item.addItem(item['line'], item['name'])
                
                logging.debug(f"✅ 범례 추가 완료: {len(legend_items)}개 항목")
            
        except Exception as ex:
            logging.error(f"❌ 범례 추가 실패: {ex}")
            logging.error(f"❌ 상세 오류: {traceback.format_exc()}")
    
    def clear_legend(self):
        """범례 제거"""
        try:
            if self.legend_item:
                self.legend_item.clear()
                self.legend_item = None
                logging.debug("✅ 범례 제거 완료")
        except Exception as ex:
            logging.error(f"❌ 범례 제거 실패: {ex}")
    
    
    
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
            logging.debug(f"X축 눈금 설정 중 오류 (무시됨): {ex}")
    
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
                    logging.debug(f"X축 레이블 설정 중 오류 (무시됨): {e}")
                    continue
            
            # pyqtgraph는 겹치는 레이블을 자동으로 숨겨 "..." 문제가 발생하지 않음
            if tics:
                axis.setTicks([tics])
                logging.debug(f"🔍 PyQtGraphWidget X축 레이블 설정 완료: {len(tics)}개 레이블 ({chart_type} 차트)")
                
        except Exception as ex:
            logging.error(f"❌ X축 레이블 설정 실패: {ex}")

# ==================== PyQtGraph 기반 실시간 차트 위젯 ====================
class PyQtGraphRealtimeWidget(QWidget):
    
    """PyQtGraph 기반 실시간 차트 위젯 - 렌더링 전용"""
    def __init__(self, parent=None):
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
        
        # 렌더링 최적화 설정
        self.render_optimization_enabled = True
        self.last_render_time = 0
        self.min_render_interval = 1.0  # 1초 간격 (실시간 업데이트)
    
    def init_pyqtgraph_widgets(self):
        """PyQtGraph 위젯 초기화"""
        try:
            # 메인 레이아웃
            layout = QVBoxLayout()
            self.setLayout(layout)
            
            # 틱 차트 위젯
            self.tic_chart_widget = PyQtGraphWidget(parent=self, title="30틱 차트")
            self.tic_chart_widget.setMinimumHeight(200)  # 최소 높이 설정
            self.tic_chart_widget.setWindowFlags(Qt.WindowType.Widget)  # 독립 창 방지
            layout.addWidget(self.tic_chart_widget, 1)
            
            # 분봉 차트 위젯
            self.minute_chart_widget = PyQtGraphWidget(parent=self, title="3분봉 차트")
            self.minute_chart_widget.setMinimumHeight(200)  # 최소 높이 설정
            self.minute_chart_widget.setWindowFlags(Qt.WindowType.Widget)  # 독립 창 방지
            layout.addWidget(self.minute_chart_widget, 1)
            
            logging.debug("PyQtGraph 위젯 초기화 완료")
            
        except BaseException as ex:
            logging.error(f"❌ PyQtGraph 위젯 초기화 실패: {ex}", exc_info=True)
            traceback.print_exc()
    
    def set_current_code(self, code):
        """현재 종목 코드 설정 및 차트 데이터 로드"""
        logging.debug(f"🔍 PyQtGraph set_current_code 호출됨: {code}")
        
        if code != self.current_code:
            self.current_code = code
            self.clear_charts()
            logging.debug(f"📊 PyQtGraph 차트 종목 변경: {code}")
            
            # 종목 코드가 설정되면 캐시에서 차트 데이터 조회하여 차트 그리기
            if code and hasattr(self.parent_window, 'chart_cache') and self.parent_window.chart_cache:
                # logging.debug(f"🔍 PyQtGraph 차트 캐시 존재 확인됨, 데이터 조회 시작: {code}")
                try:
                    cache_data = self.parent_window.chart_cache.get_cached_data(code)
                    logging.debug(f"🔍 PyQtGraph 캐시 데이터 조회 결과: {cache_data is not None}")
                    
                    if cache_data:
                        tic_data = cache_data.get('tic_data')
                        min_data = cache_data.get('min_data')
                        
                        logging.debug(f"🔍 PyQtGraph 캐시 데이터 구조: {list(cache_data.keys())}")
                        logging.debug(f"🔍 PyQtGraph 틱 데이터: {tic_data is not None}, 분봉 데이터: {min_data is not None}")
                        
                        if tic_data:
                            logging.debug(f"🔍 PyQtGraph 틱 데이터 타입: {type(tic_data)}")
                            if isinstance(tic_data, dict):
                                logging.debug(f"🔍 PyQtGraph 틱 데이터 키: {list(tic_data.keys())}")
                                if 'output' in tic_data:
                                    logging.debug(f"🔍 PyQtGraph 틱 output 길이: {len(tic_data['output']) if tic_data['output'] else 0}")
                            elif isinstance(tic_data, list):
                                logging.debug(f"🔍 PyQtGraph 틱 리스트 길이: {len(tic_data)}")
                        
                        if min_data:
                            logging.debug(f"🔍 PyQtGraph 분봉 데이터 타입: {type(min_data)}")
                            if isinstance(min_data, dict):
                                logging.debug(f"🔍 PyQtGraph 분봉 데이터 키: {list(min_data.keys())}")
                                if 'output' in min_data:
                                    logging.debug(f"🔍 PyQtGraph 분봉 output 길이: {len(min_data['output']) if min_data['output'] else 0}")
                            elif isinstance(min_data, list):
                                logging.debug(f"🔍 PyQtGraph 분봉 리스트 길이: {len(min_data)}")
                        
                        if tic_data or min_data:
                            logging.debug(f"🔍 PyQtGraph update_chart_data 호출 시작: {code}")
                            self.update_chart_data(tic_data, min_data)
                            # logging.debug(f"📊 PyQtGraph 차트 데이터 로드 완료: {code}")
                        else:
                            logging.warning(f"⚠️ PyQtGraph 차트 데이터가 없습니다: {code}")
                    else:
                        logging.warning(f"⚠️ PyQtGraph 캐시에서 차트 데이터를 찾을 수 없습니다: {code}")
                except Exception as ex:
                    logging.error(f"❌ PyQtGraph 차트 데이터 로드 실패: {code} - {ex}")
            elif code:
                logging.warning(f"⚠️ PyQtGraph 차트 캐시가 없어서 차트 데이터를 로드할 수 없습니다: {code}")
        else:
            logging.debug(f"🔍 PyQtGraph 동일한 종목 코드이므로 변경하지 않음: {code}")
    
    def clear_charts(self):
        """차트 데이터 초기화"""
        self.chart_data = {'tics': [], 'minutes': []}
        self.data_cache = {'tics': [], 'minutes': []}
        
        # 속성 존재 여부 확인 후 초기화
        if hasattr(self, 'tic_chart_widget') and self.tic_chart_widget is not None:
            self.tic_chart_widget.clear_chart()
        if hasattr(self, 'minute_chart_widget') and self.minute_chart_widget is not None:
            self.minute_chart_widget.clear_chart()
    
    def update_chart_data(self, tic_data=None, minute_data=None):
        """PyQtGraph 차트 데이터 업데이트"""
        try:
            logging.debug(f"🔍 PyQtGraph update_chart_data 호출됨 - 틱: {tic_data is not None}, 분봉: {minute_data is not None}")
            current_time = time.time()
            
            # 중복 업데이트 방지
            if current_time - self.last_update_time < self.update_interval:
                logging.debug(f"🔍 PyQtGraph 중복 업데이트 방지: {current_time - self.last_update_time:.3f}초 경과")
                return
                
            data_updated = False
            
            if tic_data:
                if len(tic_data) > self.max_tic_data_points:
                    tic_data = tic_data[-self.max_tic_data_points:]
                self.chart_data['tics'] = tic_data
                data_updated = True
                
            if minute_data:
                if len(minute_data) > self.max_minute_data_points:
                    minute_data = minute_data[-self.max_minute_data_points:]
                self.chart_data['minutes'] = minute_data
                data_updated = True
                
            if data_updated:
                self.last_update_time = current_time
                self.optimized_plot_charts()
                
        except Exception as ex:
            logging.error(f"❌ PyQtGraph 차트 데이터 업데이트 실패: {ex}")
    
    def optimized_plot_charts(self):
        """PyQtGraph 최적화된 차트 그리기"""
        try:
            logging.debug(f"🔍 PyQtGraph optimized_plot_charts 호출됨")
            current_time = time.time()
            
            # 렌더링 최적화: 너무 빈번한 렌더링 방지
            if self.render_optimization_enabled:
                if current_time - self.last_render_time < self.min_render_interval:
                    logging.debug(f"🔍 PyQtGraph 렌더링 최적화로 차트 그리기 건너뜀: {current_time - self.last_render_time:.3f}초 경과")
                    return
                self.last_render_time = current_time
            
            # 틱 차트 그리기
            if self.chart_data.get('tics'):
                self._draw_pyqtchart_tic_chart()
            
            # 분봉 차트 그리기
            if self.chart_data.get('minutes'):
                self._draw_pyqtchart_minute_chart()
                
        except Exception as ex:
            logging.error(f"❌ PyQtGraph 차트 그리기 실패: {ex}")
    
    def _draw_pyqtchart_tic_chart(self):
        """PyQtGraph 틱 차트 그리기"""
        try:
            # 위젯 초기화 확인
            if not hasattr(self, 'tic_chart_widget') or self.tic_chart_widget is None:
                logging.error("❌ PyQtGraph 틱 차트 위젯이 초기화되지 않았습니다")
                return
                
            logging.debug("🔍 PyQtGraph 틱 차트 그리기 시작")
            self.tic_chart_widget.clear_chart()
            
            # technical_indicators 변수 초기화
            if not hasattr(self, 'technical_indicators'):
                self.technical_indicators = {}
            
            # 틱 데이터 가져오기
            tic_data = self.chart_data.get('tics')
            if not tic_data:
                logging.warning("⚠️ PyQtGraph 틱 데이터가 없습니다")
                return
                
            # 데이터 처리 및 변환
            data_list = self._process_tic_data(tic_data)
            if not data_list:
                return
            
            # 차트 표시용 데이터 준비 (최대 100개)
            display_data = data_list[-100:] if len(data_list) > 100 else data_list
            logging.debug(f"🔍 틱 차트 데이터 처리: 표시 {len(display_data)}개")
            
            # 캔들스틱 데이터 생성
            candlestic_data = self._create_candlestic_data(display_data)
            if not candlestic_data:
                logging.warning("⚠️ 틱 차트 캔들스틱 데이터가 없습니다")
                return
            
            # 차트에 데이터 추가
            self.tic_chart_widget.add_candlestic_data(candlestic_data, chart_type="tic")
            logging.debug("✅ 틱 차트 캔들스틱 데이터 추가 완료")
            
            # 이동평균선 표시
            self._add_moving_averages_to_tic_chart(candlestic_data)
            
            # 차트 위젯 업데이트
            self.tic_chart_widget.update()
            self.tic_chart_widget.repaint()
            logging.debug("✅ 틱 차트 위젯 업데이트 완료")
                                          
        except Exception as ex:
            logging.error(f"❌ PyQtGraph 틱 차트 그리기 실패: {ex}")
            logging.error(f"❌ PyQtGraph 틱 차트 오류 상세: {traceback.format_exc()}")
    
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
                    logging.warning("⚠️ 틱 데이터에 필요한 키가 없음")
                    return None
        elif isinstance(tic_data, list):
            data_list = tic_data
        else:
            logging.warning(f"⚠️ 틱 데이터 형식이 예상과 다름: {type(tic_data)}")
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
            logging.debug(f"✅ 이동평균선 데이터 추출 완료: {list(ma_indicators.keys())}")
        else:
            logging.warning("⚠️ 이동평균선 데이터를 찾을 수 없습니다")
    
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
        
        logging.debug(f"🔍 API 응답 구조 변환: {len(data_list)}개 (OHLC 보정 완료)")
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
            logging.warning("⚠️ technical_indicators 변수를 찾을 수 없습니다")
            return
        
        if not isinstance(self.technical_indicators, dict):
            logging.warning(f"⚠️ technical_indicators가 딕셔너리가 아닙니다: {type(self.technical_indicators)}")
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
            logging.debug(f"✅ 틱 차트 이동평균선 표시 완료: {list(ma_indicators.keys())}")
        else:
            logging.warning("⚠️ 이동평균선 데이터를 찾을 수 없습니다")
    
    def _draw_pyqtchart_minute_chart(self):
        """PyQtGraph 분봉 차트 그리기"""
        try:
            # 위젯 초기화 확인
            if not hasattr(self, 'minute_chart_widget') or self.minute_chart_widget is None:
                logging.error("❌ PyQtGraph 분봉 차트 위젯이 초기화되지 않았습니다")
                return
                
            logging.debug("🔍 PyQtGraph 분봉 차트 그리기 시작")
            self.minute_chart_widget.clear_chart()
            
            # technical_indicators 변수 초기화
            if not hasattr(self, 'technical_indicators'):
                self.technical_indicators = {}
            
            # 분봉 데이터 가져오기
            minute_data = self.chart_data.get('minutes')
            if not minute_data:
                logging.warning("⚠️ PyQtGraph 분봉 데이터가 없습니다")
                return
                
            # 데이터 처리 및 변환
            data_list = self._process_minute_data(minute_data)
            if not data_list:
                return
            
            # 차트 표시용 데이터 준비 (최대 50개)
            display_data = data_list[-50:] if len(data_list) > 50 else data_list
            logging.debug(f"🔍 분봉 차트 데이터 처리: 표시 {len(display_data)}개")
            
            # 캔들스틱 데이터 생성
            candlestic_data = self._create_candlestic_data(display_data)
            if not candlestic_data:
                logging.warning("⚠️ 분봉 차트 캔들스틱 데이터가 없습니다")
                return
            
            # 차트에 데이터 추가
            self.minute_chart_widget.add_candlestic_data(candlestic_data, chart_type="minute")
            logging.debug("✅ 분봉 차트 캔들스틱 데이터 추가 완료")
            
            # 이동평균선 표시
            self._add_moving_averages_to_minute_chart(candlestic_data)
            
            # 차트 위젯 업데이트
            self.minute_chart_widget.update()
            self.minute_chart_widget.repaint()
            logging.debug("✅ 분봉 차트 위젯 업데이트 완료")
                                          
        except Exception as ex:
            logging.error(f"❌ PyQtGraph 분봉 차트 그리기 실패: {ex}")
            logging.error(f"❌ PyQtGraph 분봉 차트 오류 상세: {traceback.format_exc()}")
    
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
                    logging.warning("⚠️ 분봉 데이터에 필요한 키가 없음")
                    return None
        elif isinstance(minute_data, list):
            data_list = minute_data
        else:
            logging.warning(f"⚠️ 분봉 데이터 형식이 예상과 다름: {type(minute_data)}")
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
            logging.debug(f"✅ 분봉 이동평균선 데이터 추출 완료: {list(ma_indicators.keys())}")
        else:
            logging.warning("⚠️ 분봉 이동평균선 데이터를 찾을 수 없습니다")
    
    def _add_moving_averages_to_minute_chart(self, candlestic_data):
        """분봉 차트에 이동평균선 추가"""
        if not hasattr(self, 'technical_indicators') or not self.technical_indicators:
            logging.warning("⚠️ technical_indicators 변수를 찾을 수 없습니다")
            return
        
        if not isinstance(self.technical_indicators, dict):
            logging.warning(f"⚠️ technical_indicators가 딕셔너리가 아닙니다: {type(self.technical_indicators)}")
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
            logging.debug(f"✅ 분봉 차트 이동평균선 표시 완료: {list(ma_indicators.keys())}")
        else:
            logging.warning("⚠️ 이동평균선 데이터를 찾을 수 없습니다")
    
    def optimized_update_charts(self):
        """최적화된 차트 업데이트 (타이머에서 호출)"""
        if not self.current_code:
            return
            
        try:
            now = datetime.now()
            
            # 장 시작 시간(09:00) 이전에는 차트 렌더링 업데이트 중지
            market_open_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now < market_open_time:
                logging.debug(f"⏰ 장 시작 시간({market_open_time.strftime('%H:%M:%S')}) 이전이므로 차트 렌더링 업데이트를 중지합니다.")
                return

            current_time = time.time()
            
            # 업데이트 간격 제한 (성능 최적화)
            if current_time - self.last_update_time < self.update_interval:
                return
                
            # 부모 윈도우에서 최신 데이터 가져오기
            if hasattr(self.parent_window, 'chart_cache') and self.parent_window.chart_cache:
                cache_data = self.parent_window.chart_cache.get_cached_data(self.current_code)
                if cache_data:
                    self.update_chart_data(cache_data.get('tic_data'), cache_data.get('min_data'))
                    
        except Exception as ex:
            logging.error(f"❌ 최적화된 차트 업데이트 실패: {ex}")

class ChartDataCache(QObject):
    """모니터링 종목 차트 데이터 메모리 캐시 클래스"""
    
    # 시그널 정의
    data_updated = pyqtSignal(str)  # 특정 종목 데이터 업데이트
    cache_cleared = pyqtSignal()    # 캐시 전체 정리
    
    def __init__(self, trader, parent):
        try:
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
            logging.debug("🔍 API 요청 큐 시스템 초기화 완료")
            
            # QTimer 생성을 지연시켜 메인 스레드에서 실행되도록 함
            self.update_timer = None
            self.save_timer = None
            logging.debug("🔍 타이머 변수 초기화 완료")
            
            # API 시그널 연결
            self._connect_api_signals()
            async def delayed_init_timers():
                await asyncio.sleep(0.1)  # 100ms 대기
                self._initialize_timers()
            asyncio.create_task(delayed_init_timers())
            logging.debug("🔍 타이머 초기화 예약 완료 (100ms 후)")
            
            logging.debug("📊 차트 데이터 캐시 초기화 완료")
        except Exception as ex:
            logging.error(f"❌ ChartDataCache 초기화 실패: {ex}")
            logging.error(f"ChartDataCache 초기화 예외 상세: {traceback.format_exc()}")
            raise ex
    
    def _connect_api_signals(self):
        """API 제한 관리자 시그널 연결"""
        pass
    
    def collect_chart_data_async(self, code, max_retries=3):
        """비동기 차트 데이터 수집 (asyncio 기반, qasync 통합)"""
        try:
            # qasync 환경에서 메인 이벤트 루프 사용 시도
            try:
                loop = asyncio.get_running_loop()
                # 이미 실행 중인 이벤트 루프가 있으면 태스크로 실행
                task = asyncio.create_task(self._collect_chart_data_internal(code, max_retries))
                self.active_chart_tasks[code] = task
                logging.debug(f"✅ 차트 데이터 수집 태스크 시작: {code} (활성 태스크 수: {len(self.active_chart_tasks)})")
                return
            except RuntimeError:
                # 실행 중인 이벤트 루프가 없음 - ThreadPoolExecutor로 fallback
                logging.debug(f"⚠️ 실행 중인 이벤트 루프가 없어 ThreadPoolExecutor 사용: {code}")
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
                    logging.error(f"차트 데이터 수집 실행 오류: {e}")
                    return None
            
            # 별도 스레드에서 데이터 수집 실행
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_collect)
                future.result(timeout=60)  # 60초 타임아웃
            
        except Exception as ex:
            logging.error(f"❌ 비동기 차트 데이터 수집 실패: {code} - {ex}")
    
    async def _collect_chart_data_internal(self, code, max_retries=3):
        """내부 차트 데이터 수집 (asyncio 기반)"""
        try:
            logging.debug(f"📊 차트 데이터 수집 시작: {code}")
            
            # 비동기로 틱 데이터와 분봉 데이터 수집
            tic_data, min_data = await asyncio.gather(
                self._collect_tic_data_async(code, max_retries),
                self._collect_minute_data_async(code, max_retries),
                return_exceptions=True
            )
            
            # 예외 처리
            if isinstance(tic_data, Exception):
                logging.error(f"틱 데이터 수집 실패: {tic_data}")
                tic_data = None
            if isinstance(min_data, Exception):
                logging.error(f"분봉 데이터 수집 실패: {min_data}")
                min_data = None
            
            # 틱 데이터가 None인 경우 빈 딕셔너리로 초기화
            if tic_data is None:
                tic_data = {'time': [], 'open': [], 'high': [], 'low': [], 'close': [], 'volume': [], 'strength': []}
                logging.warning(f"틱 데이터가 None입니다. 빈 데이터로 초기화: {code}")
                
            # 분봉 데이터가 None인 경우 빈 딕셔너리로 초기화
            if min_data is None:
                min_data = {'time': [], 'open': [], 'high': [], 'low': [], 'close': [], 'volume': []}
                logging.warning(f"분봉 데이터가 None입니다. 빈 데이터로 초기화: {code}")
            
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
                        logging.error(f"틱 지표 계산 실패: {tic_data}")
                        tic_data = None
                    if isinstance(min_data, Exception):
                        logging.error(f"분봉 지표 계산 실패: {min_data}")
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
                logging.error(f"기술적 지표 계산 중 오류: {calc_ex}")
            
            # 메인 스레드에서 콜백 실행
            QTimer.singleShot(0, lambda: self._on_chart_data_ready(code, tic_data, min_data))
            
        except Exception as e:
            logging.error(f"차트 데이터 수집 실패 ({code}): {e}")
            QTimer.singleShot(0, lambda: self._on_chart_data_error(code, str(e)))
        finally:
            # 태스크 완료 처리
            if code in self.active_chart_tasks:
                self.active_chart_tasks.pop(code)
                logging.debug(f"✅ 차트 데이터 수집 태스크 정리 완료: {code}")
    
    async def _collect_tic_data_async(self, code, max_retries=3):
        """틱 데이터 수집 (asyncio 기반)"""
        for attempt in range(max_retries):
            try:
                # API 제한 확인 (비동기 버전)
                await ApiLimitManager.check_api_limit_and_wait_async(request_type='tic')
                
                # ThreadPoolExecutor를 사용하여 동기 API를 비동기로 실행
                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(
                    None,  # 기본 ThreadPoolExecutor 사용
                    self.trader.client.get_stock_tic_chart,
                    code,
                    30  # tic_scope
                )
                
                if data:
                    return data
                    
            except Exception as e:
                logging.warning(f"틱 데이터 수집 시도 {attempt + 1}/{max_retries} 실패: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
        
        return None
    
    async def _collect_minute_data_async(self, code, max_retries=3):
        """분봉 데이터 수집 (asyncio 기반)"""
        for attempt in range(max_retries):
            try:
                # API 제한 확인 (비동기 버전)
                await ApiLimitManager.check_api_limit_and_wait_async(request_type='minute')
                
                # ThreadPoolExecutor를 사용하여 동기 API를 비동기로 실행
                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(
                    None,  # 기본 ThreadPoolExecutor 사용
                    self.trader.client.get_stock_minute_chart,
                    code
                )
                
                if data:
                    return data
                    
            except Exception as e:
                logging.warning(f"분봉 데이터 수집 시도 {attempt + 1}/{max_retries} 실패: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
        
        return None
    
    def _on_chart_data_ready(self, code, tic_data, min_data):
        """차트 데이터 수집 완료 시그널 핸들러"""
        try:
            logging.debug(f"✅ 차트 데이터 수집 완료: {code} (tic: {tic_data is not None}, min: {min_data is not None})")
            
            # 캐시에 데이터 저장
            if code not in self.cache:
                self.cache[code] = {
                    'tic_data': None,
                    'min_data': None,
                    'last_update': None,
                    'last_save': None,
                    'previous_close': 0  # 전일종가 (한 번만 조회)
                }
                logging.debug(f"📝 {code}: 캐시 초기화")
            
            # 기술적 지표 계산은 이미 _collect_chart_data_internal에서 완료됨
            # _on_chart_data_ready는 캐시 저장 및 UI 업데이트만 담당
            
            self.cache[code]['tic_data'] = tic_data
            self.cache[code]['min_data'] = min_data
            self.cache[code]['last_update'] = datetime.now()
            
            logging.debug(f"💾 {code}: 캐시에 데이터 저장 완료 (총 캐시: {len(self.cache)}개 종목)")
            
            # 데이터 업데이트 시그널 발생
            self.data_updated.emit(code)
            
            # API 큐에서 처리된 종목을 모니터링 리스트박스에 추가
            if code in self.pending_stocks:
                stock_name = self.pending_stocks[code]
                if hasattr(self, 'parent') and self.parent:
                    # 이미 모니터링에 존재하는지 확인 (중복 추가 방지)
                    already_exists = False
                    for i in range(self.parent.monitoringBox.count()):
                        item_text = self.parent.monitoringBox.item(i).text()
                        # 종목코드 추출
                        if ' - ' in item_text:
                            existing_code = item_text.split(' - ')[0]
                        else:
                            existing_code = item_text
                        
                        if existing_code == code:
                            already_exists = True
                            logging.debug(f"ℹ️ 이미 모니터링에 존재하여 추가 건너뜀: {code} - {stock_name}")
                            break
                    
                    # 존재하지 않을 때만 추가
                    if not already_exists:
                        self.parent.monitoring_manager.add_stock_to_monitoring(code, stock_name)
                        logging.debug(f"✅ 모니터링 리스트박스에 추가 완료: {code} - {stock_name}")
                
                # pending_stocks에서 제거
                del self.pending_stocks[code]
            
            # 데이터 수집 결과 로그 (간소화)
            if not tic_data and not min_data:
                logging.warning(f"⚠️ 차트 데이터 수집 실패: {code}")
            
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 처리 실패: {code} - {ex}")
            logging.error(f"차트 데이터 처리 예외 상세: {traceback.format_exc()}")
    
    def _on_chart_data_error(self, code, error_message):
        """차트 데이터 수집 에러 시그널 핸들러"""
        try:
            logging.error(f"❌ 차트 데이터 수집 에러: {code} - {error_message}")
            
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 에러 처리 실패: {code} - {ex}")
    
    def _collect_and_save_data(self, code):
        """실제 데이터 수집 및 저장"""
        try:
            # 틱 데이터 수집
            tic_data = self.get_tic_data_from_api(code)
            
            # 분봉 데이터 수집
            min_data = self.get_min_data_from_api(code)
            
            # 부분적 성공 허용: 틱 데이터 또는 분봉 데이터 중 하나라도 있으면 저장
            if tic_data or min_data:
                # 기술적 지표 계산
                if tic_data:
                    tic_data = self._calculate_technical_indicators(tic_data, "tic")
                if min_data:
                    min_data = self._calculate_technical_indicators(min_data, "minute")
                
                # 기존 캐시의 previous_close 값 유지
                previous_close = self.cache.get(code, {}).get('previous_close', 0)
                
                self.cache[code] = {
                    'tic_data': tic_data,
                    'min_data': min_data,
                    'last_update': datetime.now(),
                    'last_save': self.cache.get(code, {}).get('last_save'),
                    'previous_close': previous_close  # 전일종가 유지
                }
            else:
                logging.warning(f"⚠️ 차트 데이터 수집 실패: {code}")
            
        except Exception as ex:
            logging.error(f"❌ 실제 데이터 수집 실패: {code} - {ex}")
            logging.error(f"데이터 수집 예외 상세: {traceback.format_exc()}")
  
    def _collect_tic_data_sync(self, code, max_retries=3):
        """동기 방식 틱 데이터 수집"""
        for attempt in range(max_retries):
            try:
                # API 요청 간격 조정
                if attempt > 0:
                    wait_time = 2 ** attempt
                    logging.debug(f"⏳ API 제한 대기 중... ({wait_time}초 후 재시도 {attempt + 1}/{max_retries})")
                    QTimer.singleShot(int(wait_time * 1000), lambda: None)  # QTimer로 대기
                
                logging.debug(f"🔧 API 틱 데이터 조회 시작: {code} (시도 {attempt + 1}/{max_retries})")
                data = self.trader.client.get_stock_tic_chart(code, tic_scope=30)
                
                if data and data.get('close'):
                    logging.debug(f"✅ 틱 데이터 조회 성공: {code} - 데이터 개수: {len(data['close'])}")
                    return data
                else:
                    logging.warning(f"⚠️ 틱 데이터가 비어있음: {code}")
                    
            except Exception as e:
                logging.error(f"❌ 틱 데이터 조회 실패: {code} (시도 {attempt + 1}/{max_retries}) - {e}")
                if attempt == max_retries - 1:
                    raise e
        
        return None
    
    def _initialize_timers(self):
        """메인 스레드에서 타이머 초기화"""
        try:
            logging.debug("🔧 차트 데이터 캐시 타이머 초기화 시작 (메인 스레드)")
            logging.debug(f"🔍 현재 스레드: {threading.current_thread().name}")
            
            # QTimer 생성 및 설정
            self.update_timer = QTimer()
            self.update_timer.timeout.connect(self.update_all_charts)
            self.save_timer = QTimer()
            self.save_timer.timeout.connect(self._trigger_async_save_to_database)
            
            # API 요청 큐 처리 타이머 생성
            self.queue_timer = QTimer()
            self.queue_timer.timeout.connect(self._process_api_queue)
            
            # 타이머 시작 (설정 가능한 주기)
            # save_timer와 queue_timer는 즉시 시작
            logging.debug("🔍 save_timer 시작 중... (1분 간격)")
            self.save_timer.start(60000)     # 1분마다 DB 저장
            logging.debug("🔍 queue_timer 시작 중... (3초 간격)")
            self.queue_timer.start(3000)     # 3초마다 큐 처리
            
            # update_timer는 모니터링 종목이 추가된 후 시작 (add_monitoring_stock에서 처리)
            logging.debug("🔍 update_timer는 모니터링 종목 추가 후 시작됩니다")
            
            logging.debug("✅ 차트 데이터 캐시 타이머 초기화 완료")
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 캐시 타이머 초기화 실패: {ex}")
            logging.error(f"타이머 초기화 예외 상세: {traceback.format_exc()}")
    
    def add_monitoring_stock(self, code):
        """모니터링 종목 추가"""
        try:            
            if code not in self.cache:
                # 전일종가 조회 (ka10100 API)
                previous_close = 0
                if hasattr(self.parent, 'login_handler') and self.parent.login_handler.kiwoom_client:
                    try:
                        stock_info = self.parent.login_handler.kiwoom_client.get_stock_info_ka10100(code)
                        if stock_info and 'lastPrice' in stock_info:
                            previous_close = int(stock_info['lastPrice'])
                            logging.info(f"📊 {code} 전일종가 조회 완료: {previous_close:,}원")
                        else:
                            logging.warning(f"⚠️ {code} 전일종가 조회 실패 - 0으로 설정")
                    except Exception as e:
                        logging.error(f"❌ {code} 전일종가 조회 중 오류: {e}")
                
                self.cache[code] = {
                    'tic_data': None,
                    'min_data': None,
                    'last_update': None,
                    'last_save': None,
                    'previous_close': previous_close  # 전일종가 (한 번만 조회)
                }
                logging.debug(f"✅ 모니터링 종목 추가 완료: {code}")
                
                # 종목코드만 저장 (API 호출 제거)
                self.pending_stocks[code] = f"종목{code}"
                
                # update_timer 시작 (첫 번째 종목이 추가될 때)
                if hasattr(self, 'update_timer') and self.update_timer:
                    if not self.update_timer.isActive():
                        # 차트 업데이트 주기는 chartdata_update_interval 사용 (기본 10초)
                        chartdata_update_interval = getattr(self.trader, 'chartdata_update_interval', 10)
                        update_interval = chartdata_update_interval * 1000  # 초 -> 밀리초 변환
                        self.update_timer.start(update_interval)
                        logging.debug(f"✅ update_timer 시작: 첫 번째 모니터링 종목 추가 (차트 데이터 업데이트: {update_interval//1000}초 간격)")
                
                # API 요청 큐에 추가
                self._add_to_api_queue(code)
            else:
                logging.debug(f"ℹ️ 모니터링 종목이 이미 존재함: {code}")
                
        except Exception as ex:
            logging.error(f"❌ 모니터링 종목 추가 실패 ({code}): {ex}")
            logging.error(f"종목 추가 예외 상세: {traceback.format_exc()}")
    
    def _add_to_api_queue(self, code):
        """API 요청 큐에 종목 추가"""
        try:
            if code not in self.api_request_queue:
                self.api_request_queue.append(code)
                
                # 종목명이 pending_stocks에 없으면 기본값 저장 (API 호출 제거)
                if code not in self.pending_stocks:
                    self.pending_stocks[code] = f"종목{code}"
                
                logging.debug(f"📋 API 요청 큐에 추가: {code} (대기 중: {len(self.api_request_queue)}개)")
            else:
                logging.debug(f"📋 API 요청 큐에 이미 존재: {code}")
        except Exception as ex:
            logging.error(f"❌ API 큐 추가 실패 ({code}): {ex}")
    
    def _process_api_queue(self):
        """API 요청 큐 처리 (3초 간격)"""
        try:
            if not self.api_request_queue or self.queue_processing:
                return
            
            # 큐 처리 시작
            self.queue_processing = True
            
            # 큐에서 첫 번째 종목 가져오기
            code = self.api_request_queue.pop(0)
            name = self.pending_stocks.get(code)  # 종목명 가져오기
            
            logging.debug(f"🔧 큐에서 데이터 수집 시작: {code} (남은 큐: {len(self.api_request_queue)}개)")
            
            # 차트 데이터 수집 (QThread에서 비동기 실행)
            self.update_single_chart(code)
            
        except Exception as ex:
            logging.error(f"❌ API 큐 처리 실패: {ex}")
        finally:
            # 큐 처리 완료
            self.queue_processing = False

    def add_stock_to_api_queue(self, code):
        """종목을 API 큐에 추가 (차트 데이터 수집 후 모니터링에 추가)"""
        try:
            # 이미 모니터링에 존재하는지 확인
            if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'monitoringBox'):
                for i in range(self.parent.monitoringBox.count()):
                    existing_code = self.parent.monitoringBox.item(i).text()

                    if existing_code == code:
                        logging.debug(f"종목이 이미 모니터링에 존재합니다: {code}")
                        return False
            
            # API 큐에 추가 (중복 제거)
            if code not in self.api_request_queue:
                self.api_request_queue.append(code)
                
                # 종목명이 pending_stocks에 없으면 기본값 저장 (API 호출 제거)
                if code not in self.pending_stocks:
                    self.pending_stocks[code] = f"종목{code}"
                
                # 큐 처리 시작 (타이머가 없으면 시작)
                if not self.queue_timer:
                    self._start_queue_processing()
                
                return True
            else:
                logging.debug(f"종목이 이미 API 큐에 존재합니다: {code}")
                return True  # 중복이지만 정상적인 상황이므로 True 반환
                
        except Exception as ex:
            already_exists = False
            if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'monitoringBox'):
                for i in range(self.parent.monitoringBox.count()):
                    existing_code = self.parent.monitoringBox.item(i).text()

                    if existing_code == code:
                        already_exists = True
                        break
            
            return False
    
    def _delayed_data_collection(self, code):
        """지연된 데이터 수집 (기존 호환성 유지)"""
        try:
            # API 요청 간격 확인
            if not self._check_api_interval():
                # API 제한으로 인해 3초 후 재시도 (람다 클로저 문제 방지)
                logging.debug(f"⏳ API 제한으로 인해 3초 후 재시도: {code}")
                QTimer.singleShot(3000, lambda c=code: self._delayed_data_collection(c))
                return
            
            logging.debug(f"🔧 지연된 데이터 수집 시작: {code}")
            self.update_single_chart(code)
        except Exception as ex:
            logging.error(f"❌ 지연된 데이터 수집 실패 ({code}): {ex}")
    
    def _check_api_interval(self):
        """API 요청 간격 확인"""
        current_time = time.time()
        
        # 마지막 요청으로부터 2초 이상 경과했는지 확인
        if current_time - self.last_api_request_time < 2.0:
            return False
        
        # API 요청 시간 업데이트
        self.last_api_request_time = current_time
        self.api_request_count += 1
        
        logging.debug(f"📊 API 요청 카운트: {self.api_request_count}")
        return True
    
    def remove_monitoring_stock(self, code):
        """모니터링 종목 제거"""
        if code in self.cache:
            del self.cache[code]
            logging.debug(f"📊 모니터링 종목 제거: {code}")
    
    def update_monitoring_stocks(self, codes):
        """모니터링 종목 리스트 업데이트"""
        try:
            logging.debug(f"🔧 모니터링 종목 리스트 업데이트 시작")
            logging.debug(f"새로운 종목 리스트: {codes}")
            
            current_codes = set(self.cache.keys())
            new_codes = set(codes)
            
            logging.debug(f"현재 캐시된 종목: {list(current_codes)}")
            logging.debug(f"새로운 종목: {list(new_codes)}")
            
            # 추가할 종목 (순차적으로 처리)
            to_add = new_codes - current_codes
            if to_add:
                logging.debug(f"추가할 종목: {list(to_add)}")
                self._add_monitoring_stocks_sequentially(list(to_add))
            
            # 제거할 종목
            to_remove = current_codes - new_codes
            if to_remove:
                logging.debug(f"제거할 종목: {list(to_remove)}")
                for code in to_remove:
                    self.remove_monitoring_stock(code)
            
            # 모니터링 종목 변경 로그
            if new_codes:
                logging.debug(f"✅ 모니터링 종목 변경 완료: {list(new_codes)}")
            else:
                logging.warning("⚠️ 모니터링 종목이 없습니다")
                
        except Exception as ex:
            logging.error(f"❌ 모니터링 종목 리스트 업데이트 실패: {ex}")
            logging.error(f"종목 리스트 업데이트 예외 상세: {traceback.format_exc()}")
    
    def _start_queue_processing(self):
        """API 큐 처리 시작"""
        try:
            if self.queue_timer:
                return  # 이미 처리 중
            
            self.queue_timer = QTimer()
            self.queue_timer.timeout.connect(self._process_api_queue)
            self.queue_timer.start(3000)  # 3초 간격으로 처리
            
        except Exception as ex:
            logging.error(f"❌ 큐 처리 시작 실패: {ex}")
    
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
    
    # 기존 순차 추가 메서드들은 큐 시스템으로 대체됨

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
            
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 업데이트 실패: {code} - {ex}")
    
    def update_all_charts(self):
        """모든 모니터링 종목 차트 데이터 업데이트 - 큐 시스템 사용"""
        try:
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

            cached_codes = list(self.cache.keys())
            logging.debug(f"🔧 전체 차트 데이터 업데이트 시작 - 캐시된 종목: {cached_codes}")
            
            if not cached_codes:
                logging.warning("⚠️ 캐시된 종목이 없습니다")
                return
            
            # 모든 종목을 큐에 추가 (중복 제거)
            added_count = 0
            for code in cached_codes:
                if code not in self.api_request_queue:
                    self.api_request_queue.append(code)
                    added_count += 1
            
            logging.debug(f"📋 {added_count}개 종목을 주기 업데이트 큐에 추가 (총 큐: {len(self.api_request_queue)}개)")
            
        except Exception as ex:
            logging.error(f"❌ 전체 차트 데이터 업데이트 실패: {ex}")
            logging.error(f"전체 업데이트 예외 상세: {traceback.format_exc()}")
    
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
            logging.error(f"ChartDataCache 데이터 조회 실패 ({code}): {ex}")
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
        """30틱봉 데이터 조회 (재시도 로직 포함)"""
        
        for attempt in range(max_retries):
            try:
                # API 요청 간격 조정 (첫 번째 시도가 아닌 경우 대기)
                if attempt > 0:
                    wait_time = 2 ** attempt  # 지수 백오프: 2초, 4초, 8초
                    logging.debug(f"⏳ API 제한 대기 중... ({wait_time}초 후 재시도 {attempt + 1}/{max_retries})")
                    # QTimer를 사용한 비동기 대기 (UI 블로킹 방지)
                    QTimer.singleShot(int(wait_time * 1000), lambda: None)
                
                logging.debug(f"🔧 API 틱 데이터 조회 시작: {code} (시도 {attempt + 1}/{max_retries})")
                data = self.trader.client.get_stock_tic_chart(code, tic_scope=30)
                
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
                    logging.error(f"❌ 틱 데이터 조회 실패 ({code}): {ex}")
                    logging.error(f"틱 데이터 조회 예외 상세: {traceback.format_exc()}")
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
                data = self.trader.client.get_stock_minute_chart(code, period=3)
                
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
                    logging.error(f"❌ 분봉 데이터 조회 실패 ({code}): {ex}")
                    logging.error(f"분봉 데이터 조회 예외 상세: {traceback.format_exc()}")
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
            
            for code, data in self.cache.items():
                tic_data = data.get('tic_data')
                min_data = data.get('min_data')
                
                logging.debug(f"🔍 {code}: tic_data={tic_data is not None}, min_data={min_data is not None}")
                
                if not tic_data or not min_data:
                    logging.warning(f"⚠️ {code}: 데이터 부족으로 저장 건너뜀 (tic: {tic_data is not None}, min: {min_data is not None})")
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
                await self.trader.db_manager.save_stock_data(code, tic_data, min_data)
                
                # 저장 시간 업데이트
                data['last_save'] = current_time
                saved_count += 1
                
                logging.debug(f"✅ {code}: DB 저장 완료")
            
            if saved_count > 0:
                logging.debug(f"📊 통합 차트 데이터 DB 저장 완료: {saved_count}개 종목")
            else:
                logging.warning("⚠️ 저장된 데이터가 없습니다")
                
        except Exception as ex:
            logging.error(f"통합 차트 데이터 DB 저장 실패: {ex}")
            logging.error(f"상세 오류: {traceback.format_exc()}")
    
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
    
    def log_ohlc_indicators_table(self, data, title, data_type):
        """OHLC와 기술적지표를 표 형태로 로그 출력"""
        try:
            times = data['time']
            opens = data['open']
            highs = data['high']
            lows = data['low']
            closes = data['close']
            
            if not closes or len(closes) == 0:
                return
            
            # 전체 데이터로 기술적지표 계산 (표시는 최근 10개만)
            sma5 = self._calculate_sma(closes, 5) if len(closes) >= 5 else []
            sma20 = self._calculate_sma(closes, 20) if len(closes) >= 20 else []
            rsi = self._calculate_rsi(closes, 14) if len(closes) >= 14 else []
            macd_result = self._calculate_macd(closes) if len(closes) >= 26 else {'macd_line': [], 'signal_line': [], 'histogram': []}
            macd_line, signal_line, histogram = macd_result.get('macd_line', []), macd_result.get('signal_line', []), macd_result.get('histogram', [])
            
            # 최근 10개 데이터만 표시
            display_count = min(10, len(closes))
            start_idx = max(0, len(closes) - display_count)
            
            # 표시할 데이터 슬라이스
            times = times[start_idx:]
            opens = opens[start_idx:]
            highs = highs[start_idx:]
            lows = lows[start_idx:]
            closes = closes[start_idx:]
            
            # 표 헤더 출력
            logging.debug("=" * 70)
            logging.debug(f"📊 {title} OHLC & 기술적지표 분석표")
            logging.debug("=" * 70)
            logging.debug(f"{'시간':<8} {'시가':<8} {'고가':<8} {'저가':<8} {'종가':<8} {'SMA5':<8} {'SMA20':<8} {'RSI':<6} {'MACD':<8} {'Signal':<8} {'Hist':<8}")
            logging.debug("-" * 70)
            
            # 각 시점별 데이터 출력
            for i in range(len(closes)):
                time_str = times[i].strftime('%H:%M:%S') if hasattr(times[i], 'strftime') else str(times[i])[-8:]
                
                # 전체 데이터에서의 실제 인덱스 계산 (표시 시작점 + 현재 인덱스)
                actual_idx = start_idx + i
                
                # 기술적지표 값 계산
                sma5_val = ""
                if sma5 and len(sma5) > actual_idx:
                    sma5_val = f"{sma5[actual_idx]:.0f}"
                
                sma20_val = ""
                if sma20 and len(sma20) > actual_idx:
                    sma20_val = f"{sma20[actual_idx]:.0f}"
                
                rsi_val = ""
                if rsi and len(rsi) > actual_idx:
                    rsi_val = f"{rsi[actual_idx]:.1f}"
                
                macd_val = ""
                if macd_line and len(macd_line) > actual_idx:
                    macd_val = f"{macd_line[actual_idx]:.2f}"
                
                signal_val = ""
                if signal_line and len(signal_line) > actual_idx:
                    signal_val = f"{signal_line[actual_idx]:.2f}"
                
                hist_val = ""
                if histogram and len(histogram) > actual_idx:
                    hist_val = f"{histogram[actual_idx]:.2f}"
                
                # 데이터 출력
                logging.debug(f"{time_str:<8} {opens[i]:<8.0f} {highs[i]:<8.0f} {lows[i]:<8.0f} {closes[i]:<8.0f} {sma5_val:<8} {sma20_val:<8} {rsi_val:<6} {macd_val:<8} {signal_val:<8} {hist_val:<8}")
            
            logging.debug("-" * 70)
            
        except Exception as ex:
            logging.error(f"OHLC 분석표 출력 실패: {ex}")

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
            logging.error(f"❌ 차트 데이터 캐시 정리 실패: {ex}")
            logging.error(f"캐시 정리 예외 상세: {traceback.format_exc()}")
    
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
                # 30틱 차트: MA5, MA20, MA60, MA120
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

class KiwoomWebSocketClient:
    """키움 웹소켓 클라이언트 (asyncio 기반) - 리팩토링된 버전"""
    
    def __init__(self, token: str, logger, is_mock: bool = False, parent=None):
        # 키움증권 예시코드에 맞춰 URL 설정
        if is_mock:
            self.uri = 'wss://mockapi.kiwoom.com:10000/api/dostk/websocket'  # 모의투자 웹소켓 URL
        else:
            self.uri = 'wss://api.kiwoom.com:10000/api/dostk/websocket'  # 실제투자 웹소켓 URL
        
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
            mode_text = "모의투자" if self.is_mock else "실제투자"
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
            logging.error(f'❌ 웹소켓 연결 오류: {e}')
            self.connected = False
            return False
    
    async def disconnect(self):
        """웹소켓 연결 해제 (키움증권 예시코드 기반)"""
        try:
            self.keep_running = False
            self.connected = False
            
            if self.websocket:
                await self.websocket.close()
                self.websocket = None
                logging.debug('✅ 웹소켓 서버와 연결이 해제되었습니다')
            
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
            
            logging.debug('✅ 웹소켓 클라이언트 완전 정리 완료')
            
        except Exception as ex:
            logging.error(f"❌ 웹소켓 연결 해제 실패: {ex}")
            logging.error(f"웹소켓 해제 에러 상세: {traceback.format_exc()}")
    
    async def run(self):
        """웹소켓 클라이언트 실행 (키움증권 예시코드 기반)"""
        try:
            # 서버에 연결하고, 메시지를 계속 받을 준비를 합니다.
            await self.connect()
            await self.receive_messages()
            
        except asyncio.CancelledError:
            logging.debug("🛑 웹소켓 클라이언트 태스크가 취소되었습니다")
            raise  # CancelledError는 다시 발생시켜야 함
        except Exception as e:
            logging.error(f"❌ 웹소켓 클라이언트 실행 중 오류: {e}")
            logging.error(f"웹소켓 실행 에러 상세: {traceback.format_exc()}")
        finally:
            logging.debug("🔌 웹소켓 클라이언트 정리 중...")
            await self.disconnect()
            logging.debug("✅ 웹소켓 클라이언트 정리 완료")

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
                    logging.debug(f'메시지 전송: {message}')
            except (json.JSONDecodeError, AttributeError):
                # JSON 파싱 실패시 기본 로그 출력
                logging.debug(f'메시지 전송: {message}')

    async def receive_messages(self):
        """서버에서 메시지 수신"""
        logging.debug("🔧 웹소켓 메시지 수신 루프 시작")
        message_count = 0
        
        while self.keep_running and self.connected:
            try:
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
                        logging.error(f'❌ 웹소켓 로그인 실패하였습니다. : {error_msg}')
                        
                        # 토큰 만료나 무효한 경우 토큰 갱신 후 재연결 시도
                        if 'Token' in error_msg or '토큰' in error_msg or '8005' in error_msg:
                            logging.info('🔄 토큰 만료로 인한 로그인 실패 - 토큰 갱신 후 재연결 시도')
                            
                            # REST 클라이언트를 통해 토큰 갱신 시도
                            if self.parent and hasattr(self.parent, 'login_handler'):
                                if hasattr(self.parent.login_handler, 'kiwoom_client'):
                                    try:
                                        # 토큰 갱신
                                        if self.parent.login_handler.kiwoom_client.get_access_token():
                                            logging.info('✅ 토큰 갱신 성공 - 웹소켓 재연결 시도')
                                            # 새로운 토큰으로 업데이트
                                            self.token = self.parent.login_handler.kiwoom_client.access_token
                                            # keep_running을 True로 설정 (재연결을 위해)
                                            self.keep_running = True
                                            # 재연결 시도
                                            await asyncio.sleep(1)  # 1초 대기
                                            if await self.connect():
                                                logging.info('✅ 웹소켓 재연결 성공')
                                            else:
                                                logging.error('❌ 웹소켓 재연결 실패')
                                                await self.disconnect()
                                        else:
                                            logging.error('❌ 토큰 갱신 실패')
                                            await self.disconnect()
                                    except Exception as token_err:
                                        logging.error(f'❌ 토큰 갱신 중 오류: {token_err}')
                                        await self.disconnect()
                                else:
                                    logging.error('❌ REST 클라이언트를 찾을 수 없습니다')
                                    await self.disconnect()
                            else:
                                logging.error('❌ 부모 윈도우나 login_handler를 찾을 수 없습니다')
                                await self.disconnect()
                        else:
                            # 토큰 문제가 아닌 다른 오류인 경우
                            await self.disconnect()
                    else:
                        mode_text = "모의투자" if self.is_mock else "실제투자"
                        logging.debug(f'✅ 웹소켓 로그인 성공하였습니다. ({mode_text} 모드)')
                        
                        # 웹소켓 연결 성공 시 post_login_setup 실행
                        try:
                            # post_login_setup을 직접 await하여 순차적으로 실행
                            if hasattr(self, 'parent') and hasattr(self.parent, 'post_login_setup'):
                                await self.parent.post_login_setup()
                                logging.debug("✅ post_login_setup 실행 완료")
                        except Exception as setup_err:
                            logging.error(f"❌ post_login_setup 실행 실패: {setup_err}")
                        
                        # 로그인 성공 후 주문체결 실시간 구독 시작
                        try:
                            await self.subscribe_order_execution()
                            logging.debug("🔔 주문체결 실시간 모니터링 시작")
                        except Exception as order_sub_err:
                            logging.error(f"❌ 주문체결 구독 실패: {order_sub_err}")
                        
                        # 로그인 성공 후 실시간 잔고 구독 시작
                        try:
                            await self.subscribe_balance()
                            logging.debug("🔔 실시간 잔고 모니터링 시작")
                            
                            # 웹소켓 준비 완료 - 이전에 조회한 REST API 잔고 데이터가 있으면 투자현황표 업데이트
                            if hasattr(self, 'parent') and self.parent:
                                try:
                                    # 부모 윈도우의 임시 보유종목 데이터 확인
                                    if hasattr(self.parent, '_pending_balance_data'):
                                        logging.info("🔄 웹소켓 준비 완료 - 임시 저장된 잔고 데이터로 투자현황표 초기화")
                                        self.parent._initialize_balance_data_from_rest_api(self.parent._pending_balance_data)
                                        delattr(self.parent, '_pending_balance_data')
                                except Exception as table_update_err:
                                    logging.error(f"❌ 투자현황표 초기화 실패: {table_update_err}")
                        except Exception as balance_sub_err:
                            logging.error(f"❌ 실시간 잔고 구독 실패: {balance_sub_err}")
                        
                        # 로그인 성공 후 시장 상태 구독 시작
                        try:
                            await self.subscribe_market_status()
                            logging.debug("🔔 시장 상태 모니터링 시작")
                        except Exception as market_sub_err:
                            logging.error(f"❌ 시장 상태 구독 실패: {market_sub_err}")

                # 메시지 유형이 PING일 경우 수신값 그대로 송신 (키움증권 예시코드 기반)
                if response.get('trnm') == 'PING':
                    await self.send_message(response)
                    continue  # PING은 더 이상 처리하지 않음
                    
                # CNSRLST 응답인 경우 조건검색 목록조회 결과 처리
                if response.get('trnm') == 'CNSRLST':
                    try:
                        # 응답 데이터 유효성 확인
                        if response is None:
                            logging.warning("⚠️ 조건검색 목록조회 응답 데이터가 None입니다")
                            continue
                        
                        if not isinstance(response, dict):
                            logging.warning(f"⚠️ 조건검색 목록조회 응답이 딕셔너리가 아닙니다: {type(response)}")
                            continue
                        
                        self.process_condition_search_list_response(response)
                    except Exception as condition_err:
                        logging.error(f"❌ 조건검색 목록조회 응답 처리 실패: {condition_err}")
                        logging.error(f"조건검색 응답 처리 에러 상세: {traceback.format_exc()}")

                # 실시간 데이터 처리
                if response.get('trnm') == 'REAL':  # 실시간 데이터
                    
                    # 실시간 데이터 처리 (예외 처리 강화)
                    try:
                        data_list = response.get('data', [])
                        if not isinstance(data_list, list):
                            logging.warning(f"실시간 데이터가 리스트가 아닙니다: {type(data_list)}")
                            continue
                        
                        # 데이터가 비어있는 경우 로그 (디버깅용)
                        if len(data_list) == 0:
                            logging.debug("실시간 데이터 수신했으나 data 리스트가 비어있습니다")
                            continue
                            
                        for data_item in data_list:
                            try:
                                if not isinstance(data_item, dict):
                                    logging.warning(f"데이터 아이템이 딕셔너리가 아닙니다: {type(data_item)}")
                                    continue
                                    
                                data_type = data_item.get('type')
                                if data_type == '00':  # 주문체결
                                    logging.debug(f"📋 주문체결 실시간 수신: {data_item.get('values', {}).get('913', '')}")
                                    try:
                                        self.process_order_execution_data(data_item)
                                    except Exception as order_err:
                                        logging.error(f"주문체결 데이터 처리 실패: {order_err}")
                                        logging.error(f"주문체결 데이터 처리 에러 상세: {traceback.format_exc()}")
                                elif data_type == '04':  # 현물잔고
                                    try:
                                        self.process_balance_data(data_item)
                                    except Exception as balance_err:
                                        logging.error(f"잔고 데이터 처리 실패: {balance_err}")
                                        logging.error(f"잔고 데이터 처리 에러 상세: {traceback.format_exc()}")
                                elif data_type == '0B':  # 주식체결
                                    try:
                                        # 비동기로 처리하여 기술적 지표 계산이 UI를 블로킹하지 않도록 함
                                        asyncio.create_task(self.process_stock_execution_data_async(data_item))
                                    except Exception as execution_err:
                                        logging.error(f"체결 데이터 처리 실패: {execution_err}")
                                        logging.error(f"체결 데이터 처리 에러 상세: {traceback.format_exc()}")
                                elif data_type == '0s':  # 시장 상태
                                    try:
                                        self.process_market_status_data(data_item)
                                    except Exception as market_err:
                                        logging.error(f"시장 상태 데이터 처리 실패: {market_err}")
                                        logging.error(f"시장 상태 데이터 처리 에러 상세: {traceback.format_exc()}")
                                elif data_type == '02':  # 조건검색 실시간 알림
                                    logging.debug(f"조건검색 실시간 알림 수신: {data_item.get('item')}")
                                    try:
                                        self.process_condition_realtime_notification(data_item)
                                    except Exception as condition_err:
                                        logging.error(f"조건검색 실시간 알림 처리 실패: {condition_err}")
                                        logging.error(f"조건검색 실시간 알림 처리 에러 상세: {traceback.format_exc()}")
                                else:
                                    logging.debug(f"알 수 없는 실시간 데이터 타입: {data_type}")
                            except Exception as data_item_err:
                                logging.error(f"실시간 데이터 아이템 처리 실패: {data_item_err}")
                                logging.error(f"데이터 아이템 처리 에러 상세: {traceback.format_exc()}")
                                continue
                        
                        # 메시지 큐에 추가 (예외 처리)
                        try:
                            self.message_queue.put(response)
                        except Exception as queue_err:
                            logging.error(f"메시지 큐 추가 실패: {queue_err}")
                            
                    except Exception as data_process_err:
                        logging.error(f"실시간 데이터 처리 실패: {data_process_err}")
                        logging.error(f"실시간 데이터 처리 에러 상세: {traceback.format_exc()}")
                        continue
                
                # 조건검색 응답 처리 (일반 요청 및 실시간 알림)
                if response.get('trnm') == 'CNSRREQ':  # 조건검색 응답
                    try:
                        # 응답 데이터 유효성 확인
                        if response is None:
                            logging.warning("⚠️ 조건검색 응답 데이터가 None입니다")
                            continue
                        
                        if not isinstance(response, dict):
                            logging.warning(f"⚠️ 조건검색 응답이 딕셔너리가 아닙니다: {type(response)}")
                            continue
                        
                        # 조건검색 응답 데이터 전체 출력
                        data_list = response.get('data')
                        if data_list is None:
                            data_list = []
                        logging.debug("조건검색 응답 수신(CNSRREQ): return_code=%s, cont_yn=%s, count=%d",
                                          response.get('return_code'),
                                          response.get('cont_yn'),
                                          len(data_list))
                        
                        # 응답 타입에 따라 분기 처리
                        search_type = response.get('search_type', '0')

                        # '급등주'는 일반 요청 응답을 무시하고 실시간만 사용
                        if search_type != '1':
                            cond_name = None
                            seq = response.get('seq')
                            try:
                                if seq and hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                                    for cond in self.parent.condition_search_list:
                                        if cond.get('seq') == seq:
                                            cond_name = cond.get('title')
                                            break
                            except Exception as _map_err:
                                logging.debug(f"조건검색 이름 매핑 실패: { _map_err }")

                            if cond_name == '급등주':
                                logging.debug("⚠️ '급등주' 조건검색 일반 응답은 무시하고 실시간만 처리합니다")
                                continue

                        if search_type == '1':  # 실시간 요청 응답
                            logging.debug("조건검색 실시간 요청 응답 처리")
                            self.process_condition_realtime_response(response)
                        else:
                            logging.debug(f"조건검색 일반 요청 응답 처리 (search_type: {search_type})")
                            self.process_condition_realtime_response(response)  # 일반 요청도 동일하게 처리
                    except Exception as condition_err:
                        logging.error(f"조건검색 응답 처리 실패: {condition_err}")
                        logging.error(f"조건검색 응답 처리 에러 상세: {traceback.format_exc()}")

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
                self.logger.error(f'JSON 파싱 오류: {e}, 메시지: {message[:200] if message else "None"}...')
                continue
            except Exception as e:
                self.logger.error(f'메시지 수신 오류: {e}')
                self.logger.error(f'메시지 수신 에러 상세: {traceback.format_exc()}')
                # 연결 종료 대신 계속 시도 (일시적 오류일 수 있음)
                self.logger.warning("메시지 수신 오류 발생, 연결 유지하고 계속 시도")
                
                # 심각한 오류인 경우 잠시 대기
                try:
                    await asyncio.sleep(1)  # 1초 대기
                except Exception as sleep_err:
                    self.logger.error(f"대기 중 오류: {sleep_err}")
                
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
            self.logger.debug(f'📡 실시간 주식체결(0B) 구독 요청: {codes} (그룹: {subscribe_data["grp_no"]})')

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
            self.logger.debug(f'실시간 주식체결 구독 해제 요청: {codes}')

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
            self.logger.error(f'❌ 주문체결 실시간 구독 요청 실패: {e}')

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
            self.logger.error(f'❌ 실시간 잔고 구독 요청 실패: {e}')

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
            self.logger.error(f'❌ 시장 상태 구독 요청 실패: {e}')

    def process_balance_data(self, data_item):
        """실시간 잔고 데이터 처리 (웹소켓용)
        주의: 이 메서드는 웹소켓을 통한 실시간 잔고 데이터를 처리합니다.
        REST API 계좌평가현황과는 별개의 데이터입니다.
        """
        try:
            # 실제 키움 API의 실시간 잔고 데이터 구조 파싱
            # data_item 구조: {'type': '04', 'item': 종목코드, 'values': {필드코드: 값}}
            raw_code = data_item.get('item', '')
            stock_code = self.parent.data_manager.normalize_stock_code(raw_code) if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'data_manager') else raw_code  # A 접두사 제거
            values = data_item.get('values', {})
            
            if stock_code and values:
                # 웹소켓 잔고 메시지 수신 로그 제거 (너무 빈번함)
                
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
                daily_realized_profit_rate = float(daily_realized_profit_rate_str) if daily_realized_profit_rate_str else 0.0
                
                # 수량이 0보다 큰 경우에만 처리
                if quantity > 0:
                    # 이전 수량을 먼저 저장 (balance_data 업데이트 전에)
                    prev_quantity = 0
                    is_sell_executed = False
                    is_new_stock = stock_code not in self.balance_data
                    
                    if not is_new_stock:
                        prev_quantity = self.balance_data[stock_code].get('quantity', 0)
                    
                    # 평가금액 및 평가손익 계산
                    evaluation_amount = quantity * current_price
                    purchase_amount = quantity * average_price
                    profit_loss = evaluation_amount - purchase_amount
                    profit_loss_rate = (profit_loss / purchase_amount * 100) if purchase_amount > 0 else 0
                    
                    # 잔고 데이터 저장 (기존 종목은 유지, 해당 종목만 업데이트)
                    self.balance_data[stock_code] = {
                        'code': stock_code,
                        'name': stock_name,
                        'quantity': quantity,
                        'average_price': average_price,
                        'current_price': current_price,
                        'evaluation_amount': evaluation_amount,
                        'purchase_amount': purchase_amount,
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
                                    trader.buy_times[stock_code] = datetime.now()
                                self.logger.debug(f"🆕 [{stock_code}] trader.holdings에 추가 (수량: {quantity}주, 매입단가: {average_price}원)")
                            else:
                                # 기존 보유 종목 수량 업데이트
                                old_quantity = trader.holdings[stock_code].get('quantity', 0)
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
                        self.logger.warning(f"⚠️ [{stock_code}] trader.holdings 동기화 실패: {sync_ex}")
                    
                    # 디버그 로그: balance_data 상태
                    if is_new_stock:
                        self.logger.debug(f"🆕 웹소켓 잔고 추가: {stock_code} ({stock_name}) - 현재 보유 종목 수: {len(self.balance_data)}")
                        self.logger.debug(f"   현재 balance_data 키 목록: {list(self.balance_data.keys())}")
                    else:
                        self.logger.debug(f"🔄 웹소켓 잔고 업데이트: {stock_code} (이전 수량: {prev_quantity}, 현재 수량: {quantity})")
                        # balance_data 키 목록 로그 제거 (불필요)
                    
                    # 중요 정보만 표시
                    self.logger.debug("=" * 70)
                    self.logger.debug(f"📊 실시간 잔고 수신: {stock_name}({stock_code})")
                    self.logger.debug("-" * 70)
                    self.logger.debug(f"  💰 현재가: {current_price:,.0f}원 | 보유수량: {quantity:,}주 | 매입단가: {average_price:,.0f}원")
                    self.logger.debug(f"  💎 평가금액: {evaluation_amount:,.0f}원 | 매입금액: {purchase_amount:,.0f}원")
                    
                    # 평가손익 표시 (색상 구분)
                    if profit_loss > 0:
                        self.logger.info(f"  📈 평가손익: +{profit_loss:,.0f}원 (+{profit_loss_rate:.2f}%)")
                    elif profit_loss < 0:
                        self.logger.info(f"  📉 평가손익: {profit_loss:,.0f}원 ({profit_loss_rate:.2f}%)")
                    else:
                        self.logger.info(f"  ➡️ 평가손익: 0원 (0.00%)")
                    
                    self.logger.debug(f"  🔢 주문가능수량: {order_available_qty:,}주")
                    
                    # 당일 거래 정보 (있는 경우에만 표시)
                    if daily_net_buy != 0:
                        self.logger.debug(f"  📊 당일순매수량: {daily_net_buy:,}주")
                        
                        # 매도 손익 정보 (매도 체결 시 강조 표시)
                    if daily_total_profit != 0:
                        profit_symbol = "📈" if daily_total_profit > 0 else "📉"
                        self.logger.debug(f"  {profit_symbol} 당일총매도손익: {daily_total_profit:,.0f}원")
                        
                    if daily_realized_profit != 0:
                        profit_symbol = "📈" if daily_realized_profit > 0 else "📉"
                        self.logger.info(f"  {profit_symbol} 당일실현손익: {daily_realized_profit:,.0f}원 ({daily_realized_profit_rate:+.2f}%)")
                    
                    self.logger.info("=" * 70)
                    
                    # 부모 윈도우를 통해 모니터링과 보유종목 리스트에 추가
                    if hasattr(self, 'parent') and self.parent:
                        try:
                            # 메인 스레드에서 실행되도록 QTimer 사용 (람다 클로저 문제 방지)
                            QTimer.singleShot(0, lambda code=stock_code, name=stock_name: self._add_stock_to_ui(code, name))
                        except Exception as ui_err:
                            self.logger.error(f"UI 업데이트 예약 실패: {ui_err}")
                else:
                    # 수량이 0인 경우 → 매도 체결 완료로 실현손익 표시 후 잔고에서 제거
                    if stock_code in self.balance_data:
                        # 디버그 로그: 제거 전 balance_data 상태
                        
                        # 이전 잔고 데이터 가져오기
                        prev_balance_info = self.balance_data.get(stock_code, {})

                        # 부분 매도 추적 목록에 해당 종목이 있는지 확인
                        is_partial_sell = False
                        if hasattr(self.parent, 'trader') and self.parent.trader:
                            for order_no, details in self.parent.trader.sell_order_details.items():
                                if details.get('code') == stock_code:
                                    is_partial_sell = True
                                    break

                        # 부분 매도가 아닌 '전량 매도'의 경우에만 여기서 알림 전송
                        if not is_partial_sell:
                            self.parent.login_handler.kiwoom_client.send_slack_notification_on_sell(
                                prev_balance_info, daily_realized_profit, daily_realized_profit_rate
                            )

                        self.logger.info(f"🔍 매도 체결 전 balance_data: {list(self.balance_data.keys())} ({len(self.balance_data)}개 종목)")
                        
                        self.logger.info("=" * 70)
                        self.logger.info(f"💰 매도 체결 완료 - 실현손익: {stock_name}({stock_code})")
                        self.logger.info("-" * 70)
                        
                        # 당일 매도손익 정보 표시
                        if daily_total_profit != 0:
                            profit_symbol = "✅" if daily_total_profit > 0 else "❌"
                            profit_text = "익절" if daily_total_profit > 0 else "손절"
                            self.logger.info(f"  {profit_symbol} 당일총매도손익: {daily_total_profit:+,.0f}원 ({profit_text})")
                        
                        if daily_realized_profit != 0:
                            profit_symbol = "✅" if daily_realized_profit > 0 else "❌"
                            profit_text = "익절" if daily_realized_profit > 0 else "손절"
                            self.logger.info(f"  {profit_symbol} 당일실현손익: {daily_realized_profit:+,.0f}원 ({daily_realized_profit_rate:+.2f}%) ({profit_text})")
                        
                        # 손익이 둘 다 0인 경우
                        if daily_total_profit == 0 and daily_realized_profit == 0:
                            self.logger.info(f"  ➖ 무손익 매도")
                        
                        self.logger.info(f"  📊 최종 보유수량: 0주 (전량 매도 완료)")
                        self.logger.info("=" * 70)
                        
                        # 잔고에서 제거
                        del self.balance_data[stock_code]
                        self.logger.info(f"✅ 잔고에서 제거 완료: {stock_code}")
                        self.logger.info(f"🔍 제거 후 balance_data: {list(self.balance_data.keys())} ({len(self.balance_data)}개 종목)")
                        
                        # trader.holdings 동기화 강화 (수량이 0일 때 제거)
                        try:
                            if self.parent and hasattr(self.parent, 'trader') and self.parent.trader:
                                trader = self.parent.trader
                                
                                # holdings에서 제거
                                if stock_code in trader.holdings:
                                    del trader.holdings[stock_code]
                                    self.logger.info(f"🗑️ [{stock_code}] trader.holdings에서 제거 (전량 매도 완료)")
                                
                                # buy_prices, buy_times도 정리
                                if stock_code in trader.buy_prices:
                                    del trader.buy_prices[stock_code]
                                if stock_code in trader.buy_times:
                                    del trader.buy_times[stock_code]
                                
                                # highest_prices도 정리
                                if stock_code in trader.highest_prices:
                                    del trader.highest_prices[stock_code]
                        except Exception as sync_ex:
                            self.logger.warning(f"⚠️ [{stock_code}] trader.holdings 동기화 실패 (전량 매도): {sync_ex}")
                        
                        # 최고가 정보도 제거 (objtrader에도 있을 수 있음)
                        if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'objtrader'):
                            if hasattr(self.parent.objtrader, 'highest_prices') and stock_code in self.parent.objtrader.highest_prices:
                                del self.parent.objtrader.highest_prices[stock_code]
                                self.logger.info(f"🗑️ {stock_code} 최고가 정보 제거 완료 (objtrader, 웹소켓 체결)")
                        
                        # UI에서도 제거 (람다 클로저 문제 방지)
                        if hasattr(self, 'parent') and self.parent:
                            QTimer.singleShot(0, lambda code=stock_code: self._remove_stock_from_ui(code))
            else:
                self.logger.warning(f"실시간 잔고 데이터 구조 오류: stock_code={stock_code}, values={values}")
                
        except Exception as e:
            self.logger.error(f"실시간 잔고 데이터 처리 실패: {e}")
            self.logger.error(f"잔고 데이터 처리 에러 상세: {traceback.format_exc()}")
    
    def process_order_execution_data(self, data_item):
        """주문체결 실시간 데이터 처리 (type '00')
        
        키움증권 웹소켓 주문체결 실시간 데이터 처리
        - 주문 접수, 체결, 취소, 거부 등의 상태 처리
        - 체결 완료시 보유종목 리스트 자동 업데이트
        """
        try:
            values = data_item.get('values', {})
            
            if not values:
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
            # 실현손익 관련 필드
            daily_realized_profit_str = values.get('990', '0')
            daily_realized_profit_rate_str = values.get('991', '0')

            reject_reason = values.get('919', '')  # 거부사유
            
            # 데이터 변환
            order_qty_int = int(order_qty) if order_qty else 0
            unfilled_qty_int = int(unfilled_qty) if unfilled_qty else 0
            exec_qty_int = int(exec_qty) if exec_qty else 0
            exec_price_float = float(exec_price) if exec_price else 0.0
            daily_realized_profit = float(daily_realized_profit_str) if daily_realized_profit_str else 0.0
            daily_realized_profit_rate = float(daily_realized_profit_rate_str) if daily_realized_profit_rate_str else 0.0
            
            # 로그 출력 (상태별)
            self.logger.info("=" * 70)
            
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
            self.logger.debug("-" * 70)
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
            
            self.logger.debug("=" * 70)
            
            # 부분 매도 주문 완료 시 슬랙 알림
            if order_status == '체결' and order_no in self.parent.trader.sell_order_details:
                order_detail = self.parent.trader.sell_order_details[order_no]
                order_detail['filled_qty'] += exec_qty_int
                
                self.logger.info(f"📊 부분 매도 체결 진행: 주문번호={order_no}, 체결량={exec_qty_int}주, 누적체결량={order_detail['filled_qty']}/{order_detail['total_qty']}")

                # 주문 수량이 모두 체결되었는지 확인
                if order_detail['filled_qty'] >= order_detail['total_qty']:
                    self.logger.info(f"🎉 부분 매도 주문 완료: {stock_name}({stock_code}) {order_detail['total_qty']}주")

                    # 추적 목록에서 제거
                    del self.parent.trader.sell_order_details[order_no]

            # 체결 완료 확인: 주문상태='체결' AND 미체결수량=0
            if order_status == '체결' and unfilled_qty_int == 0:
                self.logger.info(f"🎉 주문 체결 완료: {stock_name}({stock_code})")
                
                # 매수 체결 완료 → 보유종목 리스트에 추가 (람다 클로저 문제 방지)
                if buy_sell_flag == '2' or '매수' in order_type:

                    # '매수 주문 진행 중' 상태 해제
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader'):
                        if stock_code in self.parent.trader.pending_buy_orders:
                            self.parent.trader.pending_buy_orders.discard(stock_code)
                            self.logger.debug(f"🟢 [{stock_code}] 매수 체결 완료로 진행 중 상태 해제")

                    if hasattr(self, 'parent') and self.parent:
                        QTimer.singleShot(0, lambda code=stock_code, name=stock_name: self._add_stock_to_ui(code, name))
                
                # 매도 체결 완료 → 보유종목 리스트에서 제거 (람다 클로저 문제 방지)
                elif buy_sell_flag == '1' or '매도' in order_type:
                    self.logger.info(f"✅ 매도 체결 완료 → 보유종목에서 제거: {stock_code}")

                    # '주문 진행 중' 상태 해제
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader'):
                        if stock_code in self.parent.trader.pending_sell_orders:
                            self.parent.trader.pending_sell_orders.discard(stock_code)
                            self.logger.info(f"🟢 [{stock_code}] 매도 체결 완료로 진행 중 상태 해제")
                    
                    # 최고가 정보도 제거
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'objtrader'):
                        if hasattr(self.parent.objtrader, 'highest_prices') and stock_code in self.parent.objtrader.highest_prices:
                            del self.parent.objtrader.highest_prices[stock_code]
                            self.logger.info(f"🗑️ {stock_code} 최고가 정보 제거 완료 (주문 체결)")
                    
                    if hasattr(self, 'parent') and self.parent:
                        QTimer.singleShot(0, lambda code=stock_code: self._remove_stock_from_ui(code))
            
        except Exception as e:
            self.logger.error(f"주문체결 데이터 처리 실패: {e}")
            self.logger.error(f"주문체결 데이터 처리 에러 상세: {traceback.format_exc()}")
    
    def _add_stock_to_ui(self, stock_code, stock_name):
        """UI에 종목 추가 (메인 스레드에서 실행)"""
        try:
            if not hasattr(self, 'parent') or not self.parent:
                return
            
            # 1. 모니터링 리스트에 추가
            monitoring_exists = False
            for i in range(self.parent.monitoringBox.count()):
                item_text = self.parent.monitoringBox.item(i).text()
                # 종목코드 추출 (종목명 유무와 관계없이)
                if ' - ' in item_text:
                    existing_code = item_text.split(' - ')[0]
                else:
                    existing_code = item_text
                
                if existing_code == stock_code:
                    monitoring_exists = True
                    break
            
            if not monitoring_exists:
                self.parent.monitoring_manager.add_stock_to_monitoring(stock_code, stock_name)
                logging.info(f"✅ 모니터링 리스트에 추가: {stock_code} ({stock_name})")
            
            # 2. 보유종목 리스트에 추가
            holding_exists = False
            for i in range(self.parent.boughtBox.count()):
                item_text = self.parent.boughtBox.item(i).text()
                # 종목코드 추출 (종목명 유무와 관계없이)
                if ' - ' in item_text:
                    existing_code = item_text.split(' - ')[0]
                else:
                    existing_code = item_text
                
                if existing_code == stock_code:
                    holding_exists = True
                    break
            
            if not holding_exists:
                self.parent.boughtBox.addItem(stock_code)
                logging.info(f"✅ 보유종목 리스트에 추가: {stock_code} ({stock_name})")
            
            # 3. 투자 현황표 업데이트
            if hasattr(self.parent, 'update_stock_table'):
                # 디버그 로그: 투자 현황표 업데이트 전 balance_data 상태
                if hasattr(self, 'balance_data'):
                    logging.debug(f"🔍 투자 현황표 업데이트 전 WebSocket balance_data: {list(self.balance_data.keys())} ({len(self.balance_data)}개 종목)")
                self.parent.update_stock_table()
                
        except Exception as e:
            logging.error(f"UI 종목 추가 실패 ({stock_code}): {e}")
            logging.error(f"UI 종목 추가 에러 상세: {traceback.format_exc()}")
    
    def _remove_stock_from_ui(self, stock_code):
        """UI에서 종목 제거 (메인 스레드에서 실행)"""
        try:
            if not hasattr(self, 'parent') or not self.parent:
                return
            
            # 보유종목 리스트에서 제거
            for i in range(self.parent.boughtBox.count()):
                item_text = self.parent.boughtBox.item(i).text()
                # 종목코드 추출 (종목명 유무와 관계없이)
                if ' - ' in item_text:
                    existing_code = item_text.split(' - ')[0]
                else:
                    existing_code = item_text
                
                if existing_code == stock_code:
                    self.parent.boughtBox.takeItem(i)
                    logging.info(f"✅ 보유종목 리스트에서 제거: {stock_code}")
                    break
            
            # 투자 현황표 업데이트
            if hasattr(self.parent, 'update_stock_table'):
                self.parent.update_stock_table()
                    
        except Exception as e:
            logging.error(f"UI 종목 제거 실패 ({stock_code}): {e}")

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
                        
                        logging.debug(f"💰 실시간 체결(0B): {stock_code}, 시간={execution_time}, 가격={current_price:,.0f}원, 거래량={volume:,}, 체결강도={strength:.1f}%")
                        
                        # 체결 데이터를 딕셔너리로 생성
                        execution_info = {
                            'execution_time': execution_time,
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
            self.logger.error(f"실시간 데이터 처리 실패: {e}")
            self.logger.error(f"실시간 데이터 처리 에러 상세: {traceback.format_exc()}")
    
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
                        
                        logging.debug(f"💰 실시간 체결(0B): {stock_code}, 시간={execution_time}, 가격={current_price:,.0f}원, 거래량={volume:,}, 체결강도={strength:.1f}%")
                        
                        # 체결 데이터를 딕셔너리로 생성
                        execution_info = {
                            'execution_time': execution_time,
                            'current_price': current_price,
                            'volume': volume,
                            'strength': strength,
                        }
                        
                        # 보유 종목이면 balance_data의 현재가 업데이트
                        if stock_code in self.balance_data:
                            self._update_holding_current_price(stock_code, current_price)
                        
                        # 실시간 데이터를 차트 데이터에 추가 (비동기)
                        await self._add_realtime_data_to_chart_async(stock_code, execution_info)
                        
                        return
                    
                    else:
                        self.logger.warning(f"알 수 없는 데이터 타입: {data_type}")
                        return

                else:
                    self.logger.warning("실시간 데이터에서 종목코드를 찾을 수 없습니다")
            else:
                self.logger.warning("실시간 데이터에 item 정보가 없습니다")
                
        except Exception as e:
            self.logger.error(f"실시간 데이터 처리 실패: {e}")
            self.logger.error(f"실시간 데이터 처리 에러 상세: {traceback.format_exc()}")
    
    async def _add_realtime_data_to_chart_async(self, stock_code, realtime_data):
        """실시간 데이터를 차트 데이터에 추가 - 비동기 버전"""
        try:
            # MyWindow의 chart_cache에 접근
            if not hasattr(self, 'parent') or not self.parent:
                return
            
            if not hasattr(self.parent, 'chart_cache') or not self.parent.chart_cache:
                return
            
            chart_cache = self.parent.chart_cache
            
            # 차트 캐시에서 기존 데이터 가져오기
            cached_data = chart_cache.get_cached_data(stock_code)
            
            if not cached_data or not isinstance(cached_data, dict):                
                # 캐시가 없는 경우, 디버그 로그를 한 번만 출력하도록 관리
                if not hasattr(self, '_no_cache_logged'):
                    self._no_cache_logged = set()
                
                if stock_code not in self._no_cache_logged:
                    logging.debug(f"ℹ️ 실시간 데이터 수신({stock_code}), 차트 캐시 생성 대기 중...")
                    self._no_cache_logged.add(stock_code)
                
                return
            
            # tic_data와 min_data가 유효한지 확인
            tic_data = cached_data.get('tic_data')
            min_data = cached_data.get('min_data')
            
            if not tic_data or not isinstance(tic_data, dict):
                logging.debug(f"⚠️ 차트 데이터 추가 건너뜀: {stock_code} (틱 데이터 없음 또는 잘못된 타입)")
                return
            
            if not min_data or not isinstance(min_data, dict):
                logging.debug(f"⚠️ 차트 데이터 추가 건너뜀: {stock_code} (분봉 데이터 없음 또는 잘못된 타입)")
                return
            
            # 실시간 데이터를 틱/분봉 데이터에 추가
            self._update_tic_chart_with_realtime(stock_code, cached_data, realtime_data)
            self._update_minute_chart_with_realtime(stock_code, cached_data, realtime_data)
            
            # 차트 캐시 업데이트 (코드와 데이터를 캐시에 저장)
            cached_data['tic_data'] = cached_data.get('tic_data')
            cached_data['min_data'] = cached_data.get('min_data')
            chart_cache.cache[stock_code] = cached_data
            
            # 실시간 기술적 지표 계산 (비동기, ThreadPoolExecutor 사용)
            try:
                loop = asyncio.get_running_loop()
                # 틱 데이터와 분봉 데이터를 병렬로 계산
                if tic_data and min_data:
                    tic_data, min_data = await asyncio.gather(
                        loop.run_in_executor(None, chart_cache._calculate_technical_indicators, tic_data, "tic"),
                        loop.run_in_executor(None, chart_cache._calculate_technical_indicators, min_data, "minute"),
                        return_exceptions=True
                    )
                    # 예외 처리
                    if isinstance(tic_data, Exception):
                        logging.error(f"틱 지표 계산 실패: {tic_data}")
                        tic_data = None
                    if isinstance(min_data, Exception):
                        logging.error(f"분봉 지표 계산 실패: {min_data}")
                        min_data = None
                elif tic_data:
                    tic_data = await loop.run_in_executor(None, chart_cache._calculate_technical_indicators, tic_data, "tic")
                elif min_data:
                    min_data = await loop.run_in_executor(None, chart_cache._calculate_technical_indicators, min_data, "minute")
                
                # 계산된 결과를 캐시에 다시 저장
                if tic_data:
                    cached_data['tic_data'] = tic_data
                if min_data:
                    cached_data['min_data'] = min_data
                chart_cache.cache[stock_code] = cached_data
                
            except RuntimeError:
                # 이벤트 루프가 없으면 동기로 계산
                self._calculate_technical_indicators_for_realtime(stock_code, cached_data)
            except Exception as calc_ex:
                logging.error(f"기술적 지표 계산 중 오류: {calc_ex}")
                # 계산 실패해도 원본 데이터는 저장
                pass
            
            # 틱/분봉 데이터 개수 확인 (안전하게)
            tic_count = len(tic_data.get('close', [])) if tic_data else 0
            min_count = len(min_data.get('close', [])) if min_data else 0
            logging.debug(f"📊 차트 업데이트 완료: {stock_code} - 틱봉: {tic_count}개, 분봉: {min_count}개")
            
        except Exception as e:
            self.logger.error(f"실시간 차트 데이터 추가 실패: {e}")
            self.logger.error(f"에러 위치:\n{traceback.format_exc()}")
    
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
            
            evaluation_amount = quantity * current_price
            purchase_amount = quantity * average_price
            profit_loss = evaluation_amount - purchase_amount
            profit_loss_rate = (profit_loss / purchase_amount * 100) if purchase_amount > 0 else 0
            
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
                    if hasattr(self.parent.trader, 'holdings') and stock_code in self.parent.trader.holdings:
                        self.parent.trader.holdings[stock_code]['current_price'] = current_price
                        logging.debug(f"✅ holdings 현재가 업데이트: {stock_code} {current_price:,}원")
            
            # 투자현황표 업데이트 (throttling 적용)
            current_time = time.time()
            if hasattr(self, 'parent') and self.parent:
                if hasattr(self.parent, 'update_stock_table'):
                    # 마지막 업데이트로부터 일정 시간(0.5초)이 지난 경우에만 업데이트
                    if current_time - self._last_table_update_time >= self._table_update_interval:
                        QTimer.singleShot(0, self.parent.update_stock_table)
                        self._last_table_update_time = current_time
                    else:
                        logging.debug(f"📊 실시간 시세 반영 (표 업데이트 보류 - throttling): {stock_code} {old_price:,.0f}원 → {current_price:,.0f}원")
            
        except Exception as e:
            self.logger.error(f"보유 종목 현재가 업데이트 실패 ({stock_code}): {e}")
    
    def _add_realtime_data_to_chart(self, stock_code, realtime_data):
        """실시간 데이터를 차트 데이터에 추가"""
        try:
            # MyWindow의 chart_cache에 접근
            if not hasattr(self, 'parent') or not self.parent:
                return
            
            if not hasattr(self.parent, 'chart_cache') or not self.parent.chart_cache:
                return
            
            chart_cache = self.parent.chart_cache
            
            # 차트 캐시에서 기존 데이터 가져오기
            cached_data = chart_cache.get_cached_data(stock_code)
            
            if not cached_data or not isinstance(cached_data, dict):                
                # 캐시가 없는 경우, 디버그 로그를 한 번만 출력하도록 관리
                if not hasattr(self, '_no_cache_logged'):
                    self._no_cache_logged = set()
                
                if stock_code not in self._no_cache_logged:
                    logging.debug(f"ℹ️ 실시간 데이터 수신({stock_code}), 차트 캐시 생성 대기 중...")
                    self._no_cache_logged.add(stock_code)
                
                return
            
            # tic_data와 min_data가 유효한지 확인
            tic_data = cached_data.get('tic_data')
            min_data = cached_data.get('min_data')
            
            if not tic_data or not isinstance(tic_data, dict):
                logging.debug(f"⚠️ 차트 데이터 추가 건너뜀: {stock_code} (틱 데이터 없음 또는 잘못된 타입)")
                return
            
            if not min_data or not isinstance(min_data, dict):
                logging.debug(f"⚠️ 차트 데이터 추가 건너뜀: {stock_code} (분봉 데이터 없음 또는 잘못된 타입)")
                return
            
            # 실시간 데이터를 틱/분봉 데이터에 추가
            self._update_tic_chart_with_realtime(stock_code, cached_data, realtime_data)
            self._update_minute_chart_with_realtime(stock_code, cached_data, realtime_data)
            
            # 차트 캐시 업데이트 (코드와 데이터를 캐시에 저장)
            cached_data['tic_data'] = cached_data.get('tic_data')
            cached_data['min_data'] = cached_data.get('min_data')
            chart_cache.cache[stock_code] = cached_data
            
            # 실시간 기술적 지표 계산
            self._calculate_technical_indicators_for_realtime(stock_code, cached_data)
            
            # 틱/분봉 데이터 개수 확인 (안전하게)
            tic_count = len(tic_data.get('close', []))
            min_count = len(min_data.get('close', []))
            logging.debug(f"📊 차트 업데이트 완료: {stock_code} - 틱봉: {tic_count}개, 분봉: {min_count}개")
            
        except Exception as e:
            self.logger.error(f"실시간 차트 데이터 추가 실패: {e}")
            self.logger.error(f"에러 위치:\n{traceback.format_exc()}")
    
    def _calculate_technical_indicators_for_realtime(self, stock_code, cached_data):
        """실시간 데이터 업데이트 시 기술적 지표 계산"""
        try:
            # cached_data가 None이거나 dict가 아니면 리턴
            if not cached_data or not isinstance(cached_data, dict):
                logging.debug(f"⚠️ 기술적 지표 계산 건너뜀: {stock_code} (캐시 데이터 없음)")
                return
            
            tic_data = cached_data.get('tic_data', {})
            min_data = cached_data.get('min_data', {})
            
            if not tic_data or not min_data:
                return
            
            # chart_cache를 통해 기술적 지표 계산
            if not hasattr(self, 'parent') or not self.parent:
                return
            
            if not hasattr(self.parent, 'chart_cache') or not self.parent.chart_cache:
                return
            
            chart_cache = self.parent.chart_cache
            
            # 30틱봉 기술적 지표 계산
            if tic_data and len(tic_data.get('close', [])) > 0:
                tic_data = chart_cache._calculate_technical_indicators(tic_data, "tic")
                cached_data['tic_data'] = tic_data
            
            # 3분봉 기술적 지표 계산
            if min_data and len(min_data.get('close', [])) > 0:
                min_data = chart_cache._calculate_technical_indicators(min_data, "minute")
                cached_data['min_data'] = min_data
            
            self.logger.debug(f"📊 실시간 기술적 지표 계산 완료: {stock_code}")
            
        except Exception as e:
            self.logger.error(f"실시간 기술적 지표 계산 실패: {e}")
    
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
                tic_data['time'].append(dt)
                tic_data['open'].append(current_price)
                tic_data['high'].append(current_price)
                tic_data['low'].append(current_price)
                tic_data['close'].append(current_price)
                tic_data['volume'].append(volume)
                tic_data['strength'].append(strength)
                tic_data['last_tic_cnt'] = 1
                
                self.logger.info(f"🎯 첫 번째 30틱봉 생성: {stock_code}, 가격={current_price}")
                return
            
            if last_tic_cnt <= 30:
                # 30틱 이하이면 기존 봉 업데이트 (1~30번째 틱)
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
                
                self.logger.debug(f"틱 봉 업데이트 (틱수: {tic_data['last_tic_cnt']}/30): OHLC={tic_data['open'][last_index]}/{tic_data['high'][last_index]}/{tic_data['low'][last_index]}/{tic_data['close'][last_index]}, 거래량={tic_data['volume'][last_index]}")
                    
            else:
                # 31번째 틱부터 새로운 봉 생성
                tic_data['time'].append(dt)
                tic_data['open'].append(current_price)
                tic_data['high'].append(current_price)
                tic_data['low'].append(current_price)
                tic_data['close'].append(current_price)
                tic_data['volume'].append(volume)
                tic_data['strength'].append(strength)
                
                # 틱 카운트를 1로 리셋 (새 봉의 첫 번째 틱)
                tic_data['last_tic_cnt'] = 1              
                
                # 새 봉 데이터 로그 표시
                self._log_last_tic_bar_data(stock_code, tic_data, -1)
            
            # 최대 데이터 수 제한 (300개)
            max_data = 300
            for key in ['time', 'open', 'high', 'low', 'close', 'volume', 'strength']:
                if key in tic_data and len(tic_data[key]) > max_data:
                    tic_data[key] = tic_data[key][-max_data:]
                        
        except Exception as e:
            self.logger.error(f"틱 차트 실시간 데이터 추가 실패: {e}")
    
    def _update_minute_chart_with_realtime(self, stock_code, cached_data, realtime_data):
        """분봉 차트에 실시간 데이터 추가 (3분 = 1봉)"""
        try:
            # cached_data가 None이거나 dict가 아니면 리턴
            if not cached_data or not isinstance(cached_data, dict):
                logging.debug(f"⚠️ 분봉 차트 업데이트 건너뜀: {stock_code} (캐시 데이터 없음)")
                return
            
            min_data = cached_data.get('min_data', {})
            if not min_data:
                logging.debug(f"⚠️ 분봉 차트 업데이트 건너뜀: {stock_code} (분봉 데이터 없음)")
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
                # 첫 봉 생성
                min_data['time'].append(normalized_dt)
                min_data['open'].append(current_price)
                min_data['high'].append(current_price)
                min_data['low'].append(current_price)
                min_data['close'].append(current_price)
                min_data['volume'].append(volume)
                
                self.logger.info(f"🎯 첫 번째 3분봉 생성: {stock_code}, 시간={normalized_dt.strftime('%H:%M:%S')}, 가격={current_price}")
                return
            
            # 기존 분봉 데이터 확인
            last_time = min_data['time'][-1]
            
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
                # 새로운 봉 생성
                min_data['time'].append(normalized_dt)
                min_data['open'].append(current_price)
                min_data['high'].append(current_price)
                min_data['low'].append(current_price)
                min_data['close'].append(current_price)
                min_data['volume'].append(volume)
                
                # 새로운 3분봉 생성 로그
                logging.debug(f"🕐 새로운 3분봉 생성: {stock_code}, 시간: {normalized_dt.strftime('%H:%M:%S')}")
                
                # 새로운 3분봉 생성 시 마지막 봉 데이터 로그 표시
                self._log_last_minute_bar_data(stock_code, min_data, -1)                    
            
            # 최대 데이터 수 제한 (150개)
            max_data = 150
            for key in ['time', 'open', 'high', 'low', 'close', 'volume']:
                if key in min_data and len(min_data[key]) > max_data:
                    min_data[key] = min_data[key][-max_data:]
            
        except Exception as e:
            logging.error(f"분봉 차트 실시간 데이터 추가 실패: {e}")
    
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
            logging.error(f"분봉 데이터 로그 표시 실패: {e}")
    
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
            self.logger.error(f"틱 봉 데이터 로그 표시 실패: {e}")

    def process_condition_realtime_notification(self, data_item):
        """조건검색 실시간 알림 처리"""
        try:
            # 조건검색 실시간 알림 데이터 처리
            self.logger.debug(f"조건검색 실시간 알림 데이터: {data_item}")
            
            # 데이터 구조 확인 및 파싱
            item_data = data_item.get('item', {})
            values = data_item.get('values', {})
            
            # item_data가 문자열인 경우 처리
            if isinstance(item_data, str):
                # item이 문자열로 전달된 경우 (종목코드)
                stock_code = item_data
                condition_name = "조건검색"
            else:
                # item이 딕셔너리인 경우
                stock_code = item_data.get('code', '') if isinstance(item_data, dict) else ''
                condition_name = item_data.get('name', '조건검색') if isinstance(item_data, dict) else '조건검색'
            
            # values에서 추가 정보 추출
            action_type = None  # 편입/이탈 구분
            condition_seq = None  # 조건검색식 순번
            
            if values and isinstance(values, dict):
                stock_code = values.get('9001', stock_code)  # 종목코드
                action_type = values.get('843', 'I')         # 편입/이탈 구분 ('I'=편입, 'D'=이탈)
                condition_seq = values.get('841', '0')       # 조건검색식 순번
            else:
                action_type = 'I'  # 기본값은 편입(INSERT)
            
            if stock_code:
                # 액션 타입에 따른 처리
                if action_type == 'I':  # INSERT (편입)
                    self.logger.info(f"📈 조건검색 실시간 편입: {stock_code} ({condition_name}, seq: {condition_seq})")
                    # 부모 윈도우에 종목 추가 요청
                    if hasattr(self, 'parent') and self.parent:
                        # chart_cache를 통해 API 큐에 추가
                        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                            result = self.parent.chart_cache.add_stock_to_api_queue(stock_code)
                            if result:
                                self.logger.debug(f"✅ 조건검색 편입 종목 API 큐 추가 성공: {stock_code}")
                            else:
                                self.logger.debug(f"ℹ️ 조건검색 편입 종목 이미 존재 또는 중복: {stock_code}")
                        else:
                            self.logger.error(f"❌ chart_cache가 없습니다: {stock_code}")
                elif action_type == 'D':  # DELETE (이탈)
                    self.logger.info(f"📉 조건검색 실시간 이탈: {stock_code} ({condition_name}, seq: {condition_seq})")
                    # 부모 윈도우에서 종목 제거 요청
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'monitoring_manager'):
                        result = self.parent.monitoring_manager.remove_stock_from_monitoring(stock_code)
                        if result:
                            self.logger.debug(f"✅ 조건검색 이탈 종목 모니터링에서 제거 성공: {stock_code}")
                        else:
                            self.logger.debug(f"ℹ️ 조건검색 이탈 종목이 모니터링에 없음: {stock_code}")
                else:
                    self.logger.warning(f"⚠️ 알 수 없는 조건검색 액션 타입: {stock_code} - 액션: {action_type}")
            else:
                self.logger.warning("⚠️ 조건검색 실시간 알림에서 종목코드를 찾을 수 없습니다")
            
        except Exception as e:
            self.logger.error(f"❌ 조건검색 실시간 알림 처리 실패: {e}")
            self.logger.error(f"조건검색 알림 처리 에러 상세: {traceback.format_exc()}")

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
            elif market_operation == '3':
                self.logger.info("✅ KRX 장이 시작되었습니다! 거래 가능합니다.")
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
            self.logger.error(f"시장 상태 데이터 처리 실패: {e}")
            self.logger.error(f"시장 상태 데이터 처리 에러 상세: {traceback.format_exc()}")
    
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
            condition_list = []
            for item in data_list:
                if isinstance(item, list) and len(item) >= 2:
                    # 데이터 형태: ["seq", "title"]
                    condition_seq = item[0]
                    condition_name = item[1]
                    condition_list.append({
                        'title': condition_name,
                        'seq': condition_seq
                    })
                elif isinstance(item, dict):
                    # 딕셔너리 형태도 지원 (기존 로직)
                    condition_name = item.get('title', 'N/A')
                    condition_seq = item.get('seq', 'N/A')
                    condition_list.append({
                        'title': condition_name,
                        'seq': condition_seq
                    })
                else:
                    self.logger.warning(f"⚠️ 알 수 없는 데이터 형태: {item}")
            
            self.logger.info("📋 등록된 조건검색 목록:")            
            for condition in condition_list:
                self.logger.info(f"  - {condition['title']} (seq: {condition['seq']})")
            
            # 부모 윈도우에 조건검색 목록 전달
            if hasattr(self, 'parent') and self.parent:
                self.parent.condition_search_list = condition_list
                
                # 투자전략 콤보박스에 조건검색식 추가
                try:
                    # 기존 조건검색식 제거 (중복 방지)
                    condition_names = [condition['title'] for condition in condition_list]
                    for i in range(self.parent.comboStg.count() - 1, -1, -1):
                        item_text = self.parent.comboStg.itemText(i)
                        if item_text in condition_names:
                            self.parent.comboStg.removeItem(i)
                    
                    # 새로운 조건검색식 추가
                    added_count = 0
                    for condition in condition_list:
                        condition_text = condition['title']  # [조건검색] 접두사 제거
                        self.parent.comboStg.addItem(condition_text)
                        added_count += 1
                        self.logger.info(f"✅ 조건검색식 추가 ({added_count}/{len(condition_list)}): {condition_text}")
                    
                    # 저장된 조건검색식이 있는지 확인하고 자동 실행
                    self.logger.debug("🔍 저장된 조건검색식 자동 실행 확인 시작")
                    saved_condition_executed = self.parent.condition_search_manager.check_and_auto_execute_saved_condition()
                    
                    # 저장된 조건검색식이 없으면 첫 번째 조건검색 자동 실행
                    if not saved_condition_executed:
                        self.logger.info("🔍 저장된 조건검색식이 없어 첫 번째 조건검색 자동 실행")
                        if condition_list:
                            first_condition = condition_list[0]
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
                    self.logger.error(f"❌ 투자전략 콤보박스에 조건검색식 추가 실패: {add_ex}")
                    self.logger.error(f"조건검색식 추가 에러 상세: {traceback.format_exc()}")
            
        except Exception as e:
            self.logger.error(f"❌ 조건검색 목록조회 응답 처리 실패: {e}")
            self.logger.error(f"조건검색 응답 처리 에러 상세: {traceback.format_exc()}")
            
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
                self.logger.error(f"❌ 조건검색 실시간 요청 실패: {response.get('return_msg', '알 수 없는 오류')}")
                return
            
            # 조건검색 결과 데이터 추출
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
                        self.logger.info("🔧 부모 윈도우에 API 큐 추가 시작")
                    
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
                            for j in range(self.parent.monitoringBox.count()):
                                item_text = self.parent.monitoringBox.item(j).text()
                                if ' - ' in item_text:
                                    existing_code = item_text.split(' - ')[0]
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
                    
                    self.logger.info(f"✅ 조건검색 실시간 결과 API 큐 추가 완료: {added_count}개 종목 추가, {skipped_count}개 종목 건너뜀")
                   
                else:
                    self.logger.error("❌ 부모 윈도우가 없습니다")
            else:
                self.logger.warning("⚠️ 조건검색 실시간 요청 결과에 유효한 종목이 없습니다")
            
        except Exception as e:
            self.logger.error(f"❌ 조건검색 실시간 요청 응답 처리 실패: {e}")
            self.logger.error(f"조건검색 실시간 요청 응답 처리 에러 상세: {traceback.format_exc()}")

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
        
        # 세션 관리
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        })
        
        # 계좌 정보 (주문 시 필요)
        self.account_number = self.config.get('KIWOOM_API', 'account_number', fallback='')
        self.account_product_code = self.config.get('KIWOOM_API', 'account_product_code', fallback='01')
        
        # 연결 상태
        self.is_connected = False
        self.connection_lock = Lock()
        
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
            self.logger.error(f"설정 파일 로드 실패: {e}")
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
            self.logger.warning(f"토큰 저장 실패: {e}")
    
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
            
            # Authorization 헤더 설정
            self.session.headers.update({
                'Authorization': f'Bearer {self.access_token}'
            })
            
            self.logger.info(f"저장된 토큰 로드 성공 - 만료: {self.token_expires_at}")
            return True
            
        except Exception as e:
            self.logger.warning(f"토큰 로드 실패: {e}")
            return False

    def clear_token(self):
        """저장 토큰/메모리 토큰 완전 폐기"""
        try:
            # 세션 헤더 제거
            try:
                if 'Authorization' in self.session.headers:
                    del self.session.headers['Authorization']
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
                self.logger.debug(f"토큰 파일 삭제 실패(무시): {del_ex}")
        except Exception as ex:
            self.logger.debug(f"토큰 초기화 중 오류(무시): {ex}")
    
    def connect(self) -> bool:
        """키움 REST API 연결"""
        try:
            with self.connection_lock:
                # 저장된 토큰이 유효한지 확인
                if self.access_token and self.check_token_validity():
                    self.logger.info("저장된 토큰을 사용하여 연결")
                    self.is_connected = True
                    return True
                
                # 토큰이 없거나 만료된 경우 새로 발급
                if self.get_access_token():
                    # 새로 발급받은 토큰 저장
                    self.save_token()
                    self.is_connected = True
                    self.logger.info("키움 REST API 연결 성공")
                    return True
                else:
                    self.logger.error("키움 REST API 연결 실패")
                    return False
        except Exception as e:
            self.logger.error(f"연결 중 오류 발생: {e}")
            return False
    
    def disconnect(self):
        """키움 REST API 연결 해제"""
        try:
            # 중복 실행 방지
            if not hasattr(self, 'is_connected') or not self.is_connected:
                return
                
            with self.connection_lock:
                
                # 토큰 저장 (폐기하지 않음 - 재사용을 위해)
                if self.access_token and self.check_token_validity():
                    try:
                        self.save_token()
                        self.logger.info("토큰 저장 완료 (재사용 가능)")
                    except Exception as token_ex:
                        self.logger.warning(f"토큰 저장 중 오류 (무시됨): {token_ex}")
                
                self.is_connected = False
                # 토큰은 유지 (재사용을 위해)
                # self.access_token = None
                # self.token_expires_at = None
                
                if hasattr(self, 'logger'):
                    self.logger.info("키움 REST API 연결 해제 완료")
                
        except Exception as e:
            if hasattr(self, 'logger'):
                self.logger.error(f"연결 해제 중 오류: {e}")
            else:
                print(f"연결 해제 중 오류: {e}")
    
    def get_access_token(self) -> bool:
        """키움 REST API 접근토큰 발급"""
        try:
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
                    response = requests.post(url, headers=headers, json=auth_data, timeout=10)
                    
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
                        self.session.headers.update({
                            'Authorization': f'Bearer {self.access_token}'
                        })
                        
                        self.logger.info(f"접근토큰 발급 성공 - 토큰: {self.access_token[:10]}..., 만료: {self.token_expires_at}")
                        
                        # 새로 발급받은 토큰 저장
                        self.save_token()
                        
                        return True
                    elif response.status_code == 500:
                        self.logger.warning(f"서버 오류 발생 (시도 {attempt + 1}/{max_retries}): {response.status_code}")
                        if attempt < max_retries - 1:
                            wait_time = (attempt + 1) * 2  # 2, 4, 6초 대기
                            self.logger.info(f"{wait_time}초 후 재시도...")
                            time.sleep(wait_time)
                            continue
                    else:
                        self.logger.error(f"토큰 발급 실패: {response.status_code}")
                        self.logger.error(f"응답 헤더: {dict(response.headers)}")
                        self.logger.error(f"응답 본문: {response.text}")
                        return False
                        
                except requests.exceptions.RequestException as req_ex:
                    self.logger.warning(f"네트워크 오류 (시도 {attempt + 1}/{max_retries}): {req_ex}")
                    if attempt < max_retries - 1:
                        wait_time = (attempt + 1) * 2
                        self.logger.info(f"{wait_time}초 후 재시도...")
                        time.sleep(wait_time)
                        continue
                    else:
                        self.logger.error(f"네트워크 오류로 토큰 발급 실패: {req_ex}")
                        return False
            
            self.logger.error(f"최대 재시도 횟수 초과로 토큰 발급 실패")
            return False
                
        except Exception as e:
            self.logger.error(f"토큰 발급 중 오류: {e}")
            return False
    
    def revoke_access_token(self) -> bool:
        """OAuth 접근토큰 폐기 (au10002) - 키움 API 문서 참고"""
        try:
            if not self.access_token:
                return True
                
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
            
            response = requests.post(url, headers=headers, json=data, timeout=10)
            
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
                self.logger.warning(f"토큰 폐기 실패: {response.status_code} - {response.text}")
                return True  # 폐기 실패해도 무시 (토큰은 자동 만료됨)
                
        except Exception as e:
            self.logger.warning(f"토큰 폐기 중 오류 (무시됨): {e}")
            return True  # 폐기 실패해도 무시 (토큰은 자동 만료됨)

    def revoke_and_clear_token(self):
        """키움 au10002로 서버 토큰 폐기 후 로컬 토큰 완전 삭제"""
        try:
            self.revoke_access_token()
        finally:
            self.clear_token()
    
    def check_token_validity(self) -> bool:
        """토큰 유효성 검사"""
        if not self.access_token or not self.token_expires_at:
            self.logger.warning("토큰이 없거나 만료 시간이 설정되지 않음")
            return False
        
        # 토큰 만료 5분 전에 갱신
        if datetime.now() >= self.token_expires_at - timedelta(minutes=5):
            self.logger.info("토큰 만료 예정으로 갱신 시도")
            if self.get_access_token():
                self.logger.info("토큰 갱신 성공")
                return True
            else:
                self.logger.error("토큰 갱신 실패")
                return False
        
        return True
    
    def get_stock_current_price(self, code: str) -> Dict:
        """주식현재가 시세 조회 (실시간 주식 정보)
        
        Note: 키움 API에서 이 엔드포인트가 정상 작동하지 않을 수 있습니다.
        실패 시 호출한 쪽에서 추정가를 사용하도록 fallback 처리됩니다.
        """
        try:
            if not self.check_token_validity():
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
            
            response = requests.get(url, headers=headers, params=params, timeout=5)
            
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
            self.logger.debug(f"주식현재가 조회 실패 ({code}): {str(e)[:50]}... - fallback 처리됨")
            return {}
    
    def get_stock_info_ka10100(self, code: str) -> Dict:
        """종목정보 조회 (ka10100) - 전일종가 포함"""
        try:
            if not self.check_token_validity():
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
            
            response = requests.post(url, headers=headers, json=data, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                self.logger.debug(f"종목정보 조회 성공 ({code}): {result.get('name', 'Unknown')}, 전일종가: {result.get('lastPrice', 'N/A')}")
                return result
            else:
                self.logger.error(f"종목정보 조회 실패 ({code}): HTTP {response.status_code}")
                return {}
                
        except Exception as e:
            self.logger.error(f"종목정보 조회 중 오류 ({code}): {e}")
            return {}
    
    def get_stock_basic_info(self, code: str) -> Dict:
        """주식기본정보 조회 (ka10001)"""
        try:
            if not self.check_token_validity():
                return {}
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/stkinfo"
            
            params = {
                "code": code,
                "info_type": "basic"
            }
            
            response = self.session.get(url, params=params)
            
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"주식기본정보 조회 실패: {response.status_code}")
                return {}
                
        except Exception as e:
            self.logger.error(f"주식기본정보 조회 중 오류: {e}")
            return {}
    
    def get_stock_quote(self, code: str) -> Dict:
        """주식호가정보 조회 (ka10002)"""
        try:
            if not self.check_token_validity():
                return {}
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/stkinfo"
            
            params = {
                "code": code,
                "info_type": "quote"
            }
            
            response = self.session.get(url, params=params)
            
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"주식호가정보 조회 실패: {response.status_code}")
                return {}
                
        except Exception as e:
            self.logger.error(f"주식호가정보 조회 중 오류: {e}")
            return {}
    
    def get_stock_chart_data(self, code: str, period: str = "1m") -> pd.DataFrame:
        """주식 차트 데이터 조회"""
        try:
            if not self.check_token_validity():
                return pd.DataFrame()
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/chart"
            
            params = {
                "code": code,
                "period": period
            }
            
            response = self.session.get(url, params=params)
            
            if response.status_code == 200:
                data = response.json()
                return self._parse_chart_data(data)
            else:
                self.logger.error(f"차트 데이터 조회 실패: {response.status_code}")
                return pd.DataFrame()
                
        except Exception as e:
            self.logger.error(f"차트 데이터 조회 중 오류: {e}")
            return pd.DataFrame()
    
    def get_stock_tic_chart(self, code: str, tic_scope: int = 30, cont_yn: str = 'N', next_key: str = '') -> Dict:
        """주식 틱 차트 데이터 조회 (ka10079) - 참고 코드 기반 개선"""
        try:
            if not self.check_token_validity():
                return {}
            
            # API 요청 제한 확인 및 대기
            ApiLimitManager.check_api_limit_and_wait("틱 차트 조회", request_type="tic_chart")
            
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
            response = self.session.post(url, headers=headers, json=data)
            
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
                self.logger.error(f"틱 차트 데이터 조회 실패: {response.status_code}")
                try:
                    error_data = response.json()
                    self.logger.error(f"오류 상세: {error_data}")
                except:
                    self.logger.error(f"응답 내용: {response.text}")
                return {}
                
        except Exception as e:
            self.logger.error(f"틱 차트 데이터 조회 중 오류: {e}")
            return {}
    
    
    def get_stock_minute_chart(self, code: str, period: int = 3) -> Dict:
        """주식 분봉 차트 데이터 조회 (ka10080)"""
        try:
            if not self.check_token_validity():
                return {}
            
            # API 요청 제한 확인 및 대기
            ApiLimitManager.check_api_limit_and_wait("분봉 차트 조회", request_type="minute_chart")
            
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
            
            response = self.session.post(url, headers=headers, json=data)
            
            if response.status_code == 200:
                response_data = response.json()
                return self._parse_minute_chart_data(response_data)
            else:
                self.logger.error(f"분봉 차트 데이터 조회 실패: {response.status_code}")
                return {}
                
        except Exception as e:
            self.logger.error(f"분봉 차트 데이터 조회 중 오류: {e}")
            return {}
    
    def get_deposit_detail(self) -> Dict:
        """예수금상세현황요청 (kt00001) - 키움 REST API
        예수금, 출금가능금액, 주문가능금액 등을 조회합니다.
        """
        try:
            if not self.check_token_validity():
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
            response = requests.post(url, headers=headers, json=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                # return_code가 '0'이거나 없으면 성공으로 처리
                return_code = data.get('return_code')
                return_msg = data.get('return_msg', '')
                
                # 성공 조건: return_code가 '0'이거나 None이거나, 특정 성공 메시지가 있는 경우
                success_conditions = [
                    return_code == '0',
                    return_code is None,
                    '조회완료' in return_msg,
                    '성공' in return_msg
                ]
                
                if any(success_conditions):
                    self.logger.debug("예수금상세현황 조회 성공")
                    return data
                else:
                    self.logger.error(f"예수금상세현황 조회 실패: {return_msg}")
                    return {}
            else:
                self.logger.error(f"예수금상세현황 조회 실패: {response.status_code}")
                return {}
                
        except Exception as e:
            self.logger.error(f"예수금상세현황 조회 중 오류: {e}")
            return {}
    
    def get_acnt_balance(self) -> Dict:
        """계좌평가현황 조회 (kt00004) - 키움 REST API
        주의: 이 메서드는 REST API를 통한 일회성 조회입니다.
        실시간 잔고 데이터는 KiwoomWebSocketClient에서 처리됩니다.
        """
        try:
            if not self.check_token_validity():
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
            response = requests.post(url, headers=headers, json=params, timeout=10)
            
            
            if response.status_code == 200:
                data = response.json()

                # 응답 코드 확인
                if data.get('return_code') == 0:
                    return data
                else:
                    return_msg = data.get('return_msg', '알 수 없는 오류')
                    self.logger.error(f"계좌평가현황 조회 실패: {return_msg}")
                    return {}
            else:
                self.logger.error(f"계좌평가현황 조회 실패: {response.status_code}")
                self.logger.error(f"응답: {response.text}")
                return {}
                
        except Exception as e:
            self.logger.error(f"계좌평가현황 조회 중 오류: {e}")
            return {}

    
    def place_buy_order(self, code: str, quantity: int, price: int = 0, order_type: str = "market") -> bool:
        """매수 주문 (키움 REST API 기반) - 시장가만 지원
        
        신 REST API (kt10000) 방식 사용
        """
        try:
            if not self.check_token_validity():
                return False
            
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
                response = requests.post(url, headers=headers, json=data, timeout=10)
                
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
                        self.logger.error(f"❌ 매수 주문 실패: {error_msg}")
                        self.logger.error(f"응답: {result}")
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
                                self.revoke_and_clear_token()
                            except Exception:
                                pass
                        return False
                else:
                    self.logger.error(f"❌ 매수 주문 실패: HTTP {response.status_code}")
                    self.logger.error(f"응답: {response.text}")
                    return False
                    
            except requests.exceptions.RequestException as req_ex:
                self.logger.error(f"❌ HTTP 요청 실패: {req_ex}")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ 매수 주문 중 오류: {e}")
            self.logger.error(f"매수 주문 예외 상세: {traceback.format_exc()}")
            return False
    
    def place_sell_order(self, code: str, quantity: int, price: int = 0, order_type: str = "market") -> bool:
        """매도 주문 (키움 REST API 기반) - 시장가만 지원
        
        신 REST API (kt10001) 방식 사용
        """
        try:
            if not self.check_token_validity():
                return False
            
            # 보유 수량 체크는 호출자(sell_item)에서 이미 수행했으므로 생략
            # (REST API 호출 횟수 절약 및 중복 체크 제거)
            
            # API URL 설정
            host = 'https://mockapi.kiwoom.com' if self.is_mock else 'https://api.kiwoom.com'
            endpoint = '/api/dostk/ordr'
            url = host + endpoint
            
            # 시장가 주문으로 강제 설정
            ord_uv = ''  # 시장가는 주문단가 빈 문자열
            trde_tp = '3'  # 매매구분: 3=시장가
            
            self.logger.info(f"매도 주문: {code} {quantity}주 (시장가)")
            
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
                response = requests.post(url, headers=headers, json=data, timeout=10)
                
                # 응답 처리
                if response.status_code == 200:
                    result = response.json()
                    
                    # 응답 상태 확인
                    if result.get('return_code') == 0:
                        ord_no = result.get('ord_no', '')
                        self.last_order_no = ord_no # 마지막 주문 번호 저장
                        self.logger.info(f"✅ 매도 주문 성공: {code} {quantity}주 (주문번호: {ord_no})")
                        return True
                    else:
                        error_msg = result.get('return_msg', 'Unknown error')
                        return_code = result.get('return_code', 0)
                        
                        self.logger.error(f"❌ 매도 주문 실패: {error_msg}")
                        self.logger.error(f"응답: {result}")
                        
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
                                self.revoke_and_clear_token()
                            except Exception:
                                pass
                        
                        return False
                else:
                    self.logger.error(f"❌ 매도 주문 실패: HTTP {response.status_code}")
                    self.logger.error(f"응답: {response.text}")
                    return False
                    
            except requests.exceptions.RequestException as req_ex:
                self.logger.error(f"❌ HTTP 요청 실패: {req_ex}")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ 매도 주문 중 오류: {e}")
            self.logger.error(f"매도 주문 예외 상세: {traceback.format_exc()}")
            return False
    
    def get_order_history(self) -> List[Dict]:
        """주문 내역 조회"""
        try:
            if not self.check_token_validity():
                return []
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/ordr"
            
            response = self.session.get(url)
            
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"주문 내역 조회 실패: {response.status_code}")
                return []
                
        except Exception as e:
            self.logger.error(f"주문 내역 조회 중 오류: {e}")
            return []    

    def send_slack_notification_on_sell(self, prev_balance_info, daily_realized_profit, daily_realized_profit_rate, sold_qty=None):
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
                title = f"부분 매도 완료: {stock_name}({stock_code})"
                fallback_text = f"부분 매도: {stock_name} {sold_qty}주"
            else:
                title = f"전량 매도 완료: {stock_name}({stock_code})"
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
            
            requests.post(slack_webhook_url, json=message, timeout=5)
        except Exception as e:
            self.logger.error(f"Slack 알림 전송 실패: {e}")
            self.logger.debug(f"Slack 알림 실패 상세: {traceback.format_exc()}")
    
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
            self.logger.error(f"주식 가격 데이터 파싱 오류: {e}")
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
            self.logger.error(f"차트 데이터 파싱 오류: {e}")
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
                        self.logger.warning(f"시간 파싱 실패: {time_str}, {parse_ex}")
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
            self.logger.error(f"틱 차트 데이터 파싱 오류: {e}")
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
                        self.logger.warning(f"분봉 시간 파싱 실패: {time_str}, {parse_ex}")
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
            self.logger.error(f"분봉 차트 데이터 파싱 오류: {e}")
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
            self.logger.error(f"시장 개장 확인 중 오류: {e}")
    
    def get_stock_list(self, market: str = "KOSPI") -> List[Dict]:
        """주식 종목 리스트 조회"""
        try:
            if not self.check_token_validity():
                return []
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/stkinfo"
            
            params = {
                "list_type": "all",
                "market": market
            }
            
            response = self.session.get(url, params=params)
            
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"종목 리스트 조회 실패: {response.status_code}")
                return []
                
        except Exception as e:
            self.logger.error(f"종목 리스트 조회 중 오류: {e}")
            return []    

    def __del__(self):
        """소멸자 - 연결 해제 (중복 실행 방지)"""
        try:
            # 이미 연결이 해제되었는지 확인
            if hasattr(self, 'is_connected') and self.is_connected:
                self.disconnect()
        except Exception:
            # logger가 없거나 다른 오류가 발생해도 무시
            pass

if __name__ == "__main__":
    # QApplication이 이미 존재하는지 확인
    try:
        qasync.run(main())
    except asyncio.CancelledError:
        logging.info("프로그램이 정상적으로 종료되었습니다.")
    except Exception as e:
        logging.critical(f"메인 실행 중 치명적인 오류 발생: {e}", exc_info=True)
        # 오류 메시지 박스 표시
        app = QApplication.instance()
        if app:
            QMessageBox.critical(None, "치명적 오류", f"프로그램 실행 중 오류가 발생했습니다:\n{e}")
        sys.exit(1)
