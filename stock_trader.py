"""
키움 REST API 기반 자동매매 프로그램
크레온 플러스 API를 키움 REST API로 전면 리팩토링
"""

import sys
import ctypes

# PyQt6-WebEngine 초기화 (QApplication 생성 전에 필요)
try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    # OpenGL 컨텍스트 공유 설정 (WebEngine 사용을 위해 필요)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    # WebEngine 모듈 임포트
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineSettings, QWebEngineProfile
    WEBENGINE_AVAILABLE = True
except ImportError:
    WEBENGINE_AVAILABLE = False
except Exception:
    WEBENGINE_AVAILABLE = False

from PyQt6.QtWidgets import *
from PyQt6.QtCore import (
    QTimer, pyqtSignal, QProcess, QObject, QThread, Qt, 
    pyqtSlot, QRunnable, QThreadPool, QEventLoop
)
from PyQt6.QtGui import QIcon, QPainter, QFont, QColor, QTextCursor
from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
import qasync
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.offline as pyo
import numpy as np
from datetime import datetime, timedelta
import pandas as pd
import time
import numpy as np
import re
import mplfinance as mpf
import sqlite3
import os

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
import psutil
import configparser
import requests
import warnings
import logging
from openpyxl import Workbook
import json
from slack_sdk import WebClient
import threading
import queue
import asyncio
import websockets
from typing import Dict, List, Optional, Any
from threading import Lock
import copy
import talib
import traceback
from collections import deque

# 키움 REST API 클라이언트는 이제 통합됨 (아래에 정의)

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# QTextCursor 메타타입 등록 (Qt 오류 해결)
def register_qtextcursor_metatype():
    """QTextCursor 메타타입 등록 (PyQt6 호환)"""
    try:
        # PyQt6에서는 qRegisterMetaType 사용
        try:
            from PyQt6.QtCore import qRegisterMetaType
            qRegisterMetaType(QTextCursor, "QTextCursor")
            print("QTextCursor 메타타입 등록 성공 (PyQt6 qRegisterMetaType)")
            return True
        except ImportError:
            # qRegisterMetaType이 없는 경우 QMetaType 사용
            try:
                from PyQt6.QtCore import QMetaType
                QMetaType.registerType(QTextCursor)
                print("QTextCursor 메타타입 등록 성공 (PyQt6 QMetaType)")
                return True
            except Exception:
                # 모든 방법 실패 시 무시
                pass
    except Exception as e:
        print(f"QTextCursor 메타타입 등록 실패 (무시됨): {e}")
    
    return False

# 초기 등록 시도
register_qtextcursor_metatype()

# QTextEdit 삭제 오류 방지를 위한 추가 설정
import gc
import weakref

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

# Plotly 설정

# QPainter 관련 오류 방지를 위한 추가 설정
import os
os.environ['QT_AUTO_SCREEN_SCALE_FACTOR'] = '1'
os.environ['QT_SCALE_FACTOR'] = '1'

def init_kiwoom_client():
    """키움 REST API 클라이언트 초기화"""
    try:
        client = KiwoomRestClient('settings.ini')
        if client.connect():
            logging.info("키움 REST API 클라이언트 초기화 성공")
            return client
        else:
            logging.error("키움 REST API 클라이언트 초기화 실패")
            return None
    except Exception as ex:
        logging.error(f"키움 REST API 클라이언트 초기화 중 오류: {ex}")
        return None

def init_kiwoom_check():
    """키움 REST API 연결 및 권한 확인"""
    try:
        client = init_kiwoom_client()
        if client and client.is_connected:
            # 시장 상태 확인
            if client.is_market_open():
                logging.info("시장이 개장되어 있습니다.")
            else:
                logging.info("시장이 폐장되어 있습니다.")
            return client
        else:
            logging.error("키움 REST API 연결 실패")
            return None
    except Exception as ex:
        logging.error(f"키움 REST API 연결 확인 중 오류: {ex}")
        return None

def setup_logging():
    """로그 설정"""
    try:
        # 로그 디렉토리 생성
        log_dir = "logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        # 로그 파일명 (날짜별)
        log_filename = f"{log_dir}/kiwoom_trader_{datetime.now().strftime('%Y%m%d')}.log"
        
        # 로깅 설정
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_filename, encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        
        # root 로거의 INFO 레벨을 DEBUG로 변경
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        
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
        'tick_chart': 1.0,    # 틱 차트: 1초 간격
        'minute_chart': 0.5,  # 분봉 차트: 0.5초 간격
        'tick': 0.5,          # 틱 데이터: 0.5초 간격
        'minute': 0.2,        # 분봉 데이터: 0.2초 간격
        'default': 0.2        # 기본: 0.2초 간격
    }
    
    @classmethod
    def check_api_limit_and_wait(cls, operation_name="API 요청", rqtype=0, request_type=None):
        """API 제한 확인 및 대기 (개선된 버전)"""
        try:
            import time
            
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
                logging.debug(f"⏳ API 요청 간격 조정: {wait_time:.2f}초 대기 ({request_type})")
                # QTimer를 사용한 비동기 대기 (UI 블로킹 방지)
                QTimer.singleShot(int(wait_time * 1000), lambda: None)
            
            # 요청 시간 업데이트
            cls._last_request_time[request_type] = time.time()
            return True
            
        except Exception as ex:
            logging.error(f"API 제한 확인 중 오류: {ex}")
            return False
    
    @classmethod
    def _get_request_type(cls, operation_name):
        """요청 타입 결정"""
        if '틱' in operation_name or 'tick' in operation_name.lower():
            return 'tick_chart'
        elif '분봉' in operation_name or 'minute' in operation_name.lower():
            return 'minute_chart'
        else:
            return 'default'
    
    @classmethod
    def reset_request_times(cls):
        """요청 시간 기록 초기화"""
        cls._last_request_time.clear()
        logging.info("🔄 API 요청 시간 기록 초기화 완료")

# ==================== 로그 핸들러 ====================
class QTextEditLogger(logging.Handler):
    """QTextEdit에 로그를 출력하는 핸들러 (스레드 안전)"""
    
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget
        
    def emit(self, record):
        try:
            # QTextEdit 위젯이 유효한지 더 강화된 검사
            if not self.text_widget:
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
    
    def __init__(self, db_path="vi_stock_data.db"):
        self.db_path = db_path
        # 비동기 초기화는 별도로 호출해야 함
        # self.init_database()  # 비동기 메서드이므로 직접 호출 불가
    
    async def init_database(self):
        """데이터베이스 초기화 (비동기 I/O)"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.cursor()
            
            # 주식 데이터 테이블
                await cursor.execute('''
                CREATE TABLE IF NOT EXISTS stock_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    datetime TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume INTEGER,
                    UNIQUE(code, datetime)
                )
            ''')
            
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
            
                # 백테스팅용 틱 데이터 테이블
                await cursor.execute('''
                    CREATE TABLE IF NOT EXISTS tick_data (
                        code TEXT,
                        timestamp TEXT,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume INTEGER,
                        last_tic_cnt TEXT,
                        created_at TEXT,
                        PRIMARY KEY (code, timestamp)
                    )
                ''')                
                
                # 백테스팅용 분봉 데이터 테이블
                await cursor.execute('''
                    CREATE TABLE IF NOT EXISTS minute_data (
                        code TEXT,
                        timestamp TEXT,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume INTEGER,
                        created_at TEXT,
                        PRIMARY KEY (code, timestamp)
                    )
                ''')
                
                await conn.commit()
            
            logging.info("데이터베이스 초기화 완료")
            
        except Exception as ex:
            logging.error(f"데이터베이스 초기화 실패: {ex}")
            # 동기 방식으로 폴백
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                # 동기 방식으로 테이블 생성
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS stock_data (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT NOT NULL,
                        datetime TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume INTEGER,
                        UNIQUE(code, datetime)
                    )
                ''')
                conn.commit()
                conn.close()
                logging.info("데이터베이스 초기화 폴백 완료")
            except Exception as fallback_ex:
                logging.error(f"데이터베이스 초기화 폴백 실패: {fallback_ex}")
    
    async def save_stock_data(self, code, data_list):
        """주식 데이터 저장 (비동기 I/O)"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.cursor()
                
                for data in data_list:
                    await cursor.execute('''
                        INSERT OR REPLACE INTO stock_data 
                        (code, datetime, open, high, low, close, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        code,
                        data.get('datetime', ''),
                        data.get('open', 0),
                        data.get('high', 0),
                        data.get('low', 0),
                        data.get('close', 0),
                        data.get('volume', 0)
                    ))
                
                await conn.commit()
            
        except Exception as ex:
            logging.error(f"주식 데이터 저장 실패: {ex}")
            # 동기 방식으로 폴백
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                for data in data_list:
                    cursor.execute('''
                        INSERT OR REPLACE INTO stock_data 
                        (code, datetime, open, high, low, close, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        code,
                        data.get('datetime', ''),
                        data.get('open', 0),
                        data.get('high', 0),
                        data.get('low', 0),
                        data.get('close', 0),
                        data.get('volume', 0)
                    ))
                conn.commit()
                conn.close()
            except Exception as fallback_ex:
                logging.error(f"주식 데이터 저장 폴백 실패: {fallback_ex}")
    
    async def save_trade_record(self, code, datetime_str, order_type, quantity, price, strategy=""):
        """매매 기록 저장 (비동기 I/O)"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.cursor()
            
            amount = quantity * price
            
            await cursor.execute('''
                INSERT INTO trade_records 
                (code, datetime, order_type, quantity, price, amount, strategy)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (code, datetime_str, order_type, quantity, price, amount, strategy))
            
            await conn.commit()
            
            logging.info(f"매매 기록 저장: {code} {order_type} {quantity}주 @ {price}")
            
        except Exception as ex:
            logging.error(f"매매 기록 저장 실패: {ex}")
            # 동기 방식으로 폴백
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                amount = quantity * price
                cursor.execute('''
                    INSERT INTO trade_records 
                    (code, datetime, order_type, quantity, price, amount, strategy)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (code, datetime_str, order_type, quantity, price, amount, strategy))
                conn.commit()
                conn.close()
            except Exception as fallback_ex:
                logging.error(f"매매 기록 저장 폴백 실패: {fallback_ex}")
    
    async def save_tick_data(self, code, tick_data):
        """틱 데이터 저장 (비동기 I/O)"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.cursor()
                
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                # 틱 데이터 저장
                times = tick_data.get('time', [])
                opens = tick_data.get('open', [])
                highs = tick_data.get('high', [])
                lows = tick_data.get('low', [])
                closes = tick_data.get('close', [])
                volumes = tick_data.get('volume', [])
                last_tic_cnts = tick_data.get('last_tic_cnt', [])
                
                for i in range(len(times)):
                    await cursor.execute('''
                        INSERT OR REPLACE INTO tick_data 
                        (code, timestamp, open, high, low, close, volume, last_tic_cnt, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        code,
                        times[i],
                        opens[i] if i < len(opens) else 0,
                        highs[i] if i < len(highs) else 0,
                        lows[i] if i < len(lows) else 0,
                        closes[i] if i < len(closes) else 0,
                        volumes[i] if i < len(volumes) else 0,
                        last_tic_cnts[i] if i < len(last_tic_cnts) else '',
                        current_time
                    ))
                
                await conn.commit()
            
            logging.debug(f"틱 데이터 저장 완료: {code} ({len(times)}개)")
            
        except Exception as ex:
            logging.error(f"틱 데이터 저장 실패 ({code}): {ex}")
            # 동기 방식으로 폴백
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                times = tick_data.get('time', [])
                opens = tick_data.get('open', [])
                highs = tick_data.get('high', [])
                lows = tick_data.get('low', [])
                closes = tick_data.get('close', [])
                volumes = tick_data.get('volume', [])
                last_tic_cnts = tick_data.get('last_tic_cnt', [])
                
                for i in range(len(times)):
                    cursor.execute('''
                        INSERT OR REPLACE INTO tick_data 
                        (code, timestamp, open, high, low, close, volume, last_tic_cnt, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        code,
                        times[i],
                        opens[i] if i < len(opens) else 0,
                        highs[i] if i < len(highs) else 0,
                        lows[i] if i < len(lows) else 0,
                        closes[i] if i < len(closes) else 0,
                        volumes[i] if i < len(volumes) else 0,
                        last_tic_cnts[i] if i < len(last_tic_cnts) else '',
                        current_time
                    ))
                conn.commit()
                conn.close()
            except Exception as fallback_ex:
                logging.error(f"틱 데이터 저장 폴백 실패 ({code}): {fallback_ex}")
    
    async def save_minute_data(self, code, min_data):
        """분봉 데이터 저장 (비동기 I/O)"""
        try:
            import aiosqlite
            
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.cursor()
                
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                # 분봉 데이터 저장
                times = min_data.get('time', [])
                opens = min_data.get('open', [])
                highs = min_data.get('high', [])
                lows = min_data.get('low', [])
                closes = min_data.get('close', [])
                volumes = min_data.get('volume', [])
                
                for i in range(len(times)):
                    await cursor.execute('''
                        INSERT OR REPLACE INTO minute_data 
                        (code, timestamp, open, high, low, close, volume, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        code,
                        times[i],
                        opens[i] if i < len(opens) else 0,
                        highs[i] if i < len(highs) else 0,
                        lows[i] if i < len(lows) else 0,
                        closes[i] if i < len(closes) else 0,
                        volumes[i] if i < len(volumes) else 0,
                        current_time
                    ))
                
                await conn.commit()
            
            logging.debug(f"분봉 데이터 저장 완료: {code} ({len(times)}개)")
            
        except Exception as ex:
            logging.error(f"분봉 데이터 저장 실패 ({code}): {ex}")
            # 동기 방식으로 폴백
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                times = min_data.get('time', [])
                opens = min_data.get('open', [])
                highs = min_data.get('high', [])
                lows = min_data.get('low', [])
                closes = min_data.get('close', [])
                volumes = min_data.get('volume', [])
                
                for i in range(len(times)):
                    cursor.execute('''
                        INSERT OR REPLACE INTO minute_data 
                        (code, timestamp, open, high, low, close, volume, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        code,
                        times[i],
                        opens[i] if i < len(opens) else 0,
                        highs[i] if i < len(highs) else 0,
                        lows[i] if i < len(lows) else 0,
                        closes[i] if i < len(closes) else 0,
                        volumes[i] if i < len(volumes) else 0,
                        current_time
                    ))
                conn.commit()
                conn.close()
            except Exception as fallback_ex:
                logging.error(f"분봉 데이터 저장 폴백 실패 ({code}): {fallback_ex}")

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
        
        # QTextCursor 메타타입 등록 (신호 emit 시 필요)
        register_qtextcursor_metatype()
    
    def _init_database_async(self):
        """비동기 데이터베이스 초기화 트리거"""
        try:
            import asyncio
            import concurrent.futures
            
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
        
        # 포트폴리오 관리
        self.holdings = {}  # 보유 종목
        self.buy_prices = {}  # 매수 가격
        self.buy_times = {}  # 매수 시간
        self.highest_prices = {}  # 최고가 추적
        
        # 설정 로드
        self.load_settings()
        
        # 타이머 설정
        self.setup_timers()
        
        logging.info(f"키움 트레이더 초기화 완료 (목표 매수 종목 수: {self.buycount})")
    
    def load_settings(self):
        """설정 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # 매매 설정
            self.evaluation_interval = config.getint('TRADING', 'evaluation_interval', fallback=5)
            self.event_based_evaluation = config.getboolean('TRADING', 'event_based_evaluation', fallback=True)
            self.min_evaluation_gap = config.getint('TRADING', 'min_evaluation_gap', fallback=3)
            
            # 데이터 저장 설정
            self.data_saving_interval = config.getint('DATA_SAVING', 'interval_seconds', fallback=5)
            
            logging.info("설정 로드 완료")
            
        except Exception as ex:
            logging.error(f"설정 로드 실패: {ex}")
    
    def setup_timers(self):
        """타이머 설정"""
        # 데이터 저장 타이머만 유지
        self.data_save_timer = QTimer()
        self.data_save_timer.timeout.connect(self.save_market_data)
        self.data_save_timer.start(self.data_saving_interval * 1000)
    
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
        """현재가 조회"""
        try:
            price_data = self.client.get_stock_current_price(code)
            return price_data.get('current_price', 0)
        except Exception as ex:
            logging.error(f"현재가 조회 실패 ({code}): {ex}")
            return 0
    
    def place_buy_order(self, code, quantity, price=0, strategy=""):
        """매수 주문 (키움 REST API 기반)"""
        try:
            # 키움 REST API를 통한 매수 주문
            success = self.client.place_buy_order(code, quantity, price)
            
            if success:
                # 매수 기록 저장
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.db_manager.save_trade_record(code, current_time, "buy", quantity, price, strategy)
                
                # 포트폴리오 업데이트
                self.buy_prices[code] = price if price > 0 else self.get_current_price(code)
                self.buy_times[code] = datetime.now()
                self.highest_prices[code] = self.buy_prices[code]
                
                # 웹소켓 기능이 제거됨 - 별도로 관리됨
                
                self.signal_order_result.emit(code, "buy", quantity, price, True)
                logging.info(f"✅ 매수 주문 성공: {code} {quantity}주 (키움 REST API)")
                return True
            else:
                self.signal_order_result.emit(code, "buy", quantity, price, False)
                logging.error(f"❌ 매수 주문 실패: {code}")
                return False
                
        except Exception as ex:
            logging.error(f"❌ 매수 주문 중 오류 ({code}): {ex}")
            self.signal_order_result.emit(code, "buy", quantity, price, False)
            return False
    
    def place_sell_order(self, code, quantity, price=0, strategy=""):
        """매도 주문 (키움 REST API 기반)"""
        try:
            # 키움 REST API를 통한 매도 주문
            success = self.client.place_sell_order(code, quantity, price)
            
            if success:
                # 매도 기록 저장
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                sell_price = price if price > 0 else self.get_current_price(code)
                self.db_manager.save_trade_record(code, current_time, "sell", quantity, sell_price, strategy)
                
                # 포트폴리오는 실시간 잔고 데이터가 자동으로 관리함
                # (매도 체결 시 웹소켓을 통해 자동으로 보유 종목에서 제거됨)
                
                self.signal_order_result.emit(code, "sell", quantity, price, True)
                logging.info(f"✅ 매도 주문 성공: {code} {quantity}주 (키움 REST API)")
                return True
            else:
                self.signal_order_result.emit(code, "sell", quantity, price, False)
                logging.error(f"❌ 매도 주문 실패: {code}")
                return False
                
        except Exception as ex:
            logging.error(f"❌ 매도 주문 중 오류 ({code}): {ex}")
            self.signal_order_result.emit(code, "sell", quantity, price, False)
            return False
    
    def save_market_data(self):
        """시장 데이터 저장"""
        try:
            # 보유 종목들의 데이터 저장
            for code in self.holdings.keys():
                chart_data = self.client.get_stock_chart_data(code, "1m", 1)
                if not chart_data.empty:
                    data_list = []
                    for idx, row in chart_data.iterrows():
                        data_list.append({
                            'datetime': idx.strftime('%Y-%m-%d %H:%M:%S'),
                            'open': row.get('open', 0),
                            'high': row.get('high', 0),
                            'low': row.get('low', 0),
                            'close': row.get('close', 0),
                            'volume': row.get('volume', 0)
                        })
                    
                    self.db_manager.save_stock_data(code, data_list)
                    
        except Exception as ex:
            logging.error(f"시장 데이터 저장 실패: {ex}")
    
    def get_portfolio_status(self):
        """포트폴리오 상태 조회"""
        try:
            portfolio = {
                'holdings': self.holdings.copy(),
                'buy_prices': self.buy_prices.copy(),
                'buy_times': self.buy_times.copy(),
                'highest_prices': self.highest_prices.copy(),
                'total_holdings': len(self.holdings),
                'max_holdings': self.buycount
            }
            return portfolio
        except Exception as ex:
            logging.error(f"포트폴리오 상태 조회 실패: {ex}")
            return {}

# ==================== 키움 전략 클래스 ====================
class KiwoomStrategy(QObject):
    """키움 REST API 기반 전략 클래스"""
    
    # 시그널 정의
    signal_strategy_result = pyqtSignal(str, str, dict)  # code, action, data
    clear_signal = pyqtSignal()
    
    def __init__(self, trader, main_window=None):
        super().__init__()
        self.trader = trader
        self.client = trader.client
        self.db_manager = trader.db_manager
        self.main_window = main_window
        
        # QTextCursor 메타타입 등록 (신호 emit 시 필요)
        register_qtextcursor_metatype()
        
        # 전략 설정 로드
        self.load_strategy_config()
            
    def load_strategy_config(self):
        """전략 설정 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # 현재 전략 로드
            self.current_strategy = config.get('SETTINGS', 'last_strategy', fallback='통합 전략')
            
            # 전략별 설정 로드
            self.strategy_config = {}
            for section in config.sections():
                if section in ['VI 발동', '급등주', '갭상승', '통합 전략']:
                    self.strategy_config[section] = dict(config.items(section))
            
            logging.info(f"전략 설정 로드 완료: {self.current_strategy}")
            
        except Exception as ex:
            logging.error(f"전략 설정 로드 실패: {ex}")

    def display_realtime_price_info(self, code, data_item):
        """실시간 시세 정보를 로그에 표시"""
        try:
            # 종목명 조회
            stock_name = self.get_stock_name_by_code(code)
            
            # 시세 정보 추출
            current_price = self.safe_int(data_item.get('prpr', 0))  # 현재가
            open_price = self.safe_int(data_item.get('oprc', 0))     # 시가
            high_price = self.safe_int(data_item.get('hgpr', 0))     # 고가
            low_price = self.safe_int(data_item.get('lwpr', 0))      # 저가
            volume = self.safe_int(data_item.get('acml_vol', 0))     # 누적거래량
            change_rate = self.safe_float(data_item.get('prdy_vrss_ctrt', 0))  # 전일대비등락률
            change_amount = self.safe_int(data_item.get('prdy_vrss', 0))        # 전일대비등락폭
            
            # 등락 표시
            if change_rate > 0:
                change_symbol = "📈"
                change_color = "상승"
            elif change_rate < 0:
                change_symbol = "📉"
                change_color = "하락"
            else:
                change_symbol = "📊"
                change_color = "보합"
            
            # 실시간 시세 정보 로그 출력
            logging.info(f"🔴 {stock_name} ({code}) 실시간 시세")
            logging.info(f"   💰 현재가: {current_price:,}원 {change_symbol} {change_amount:+,}원 ({change_rate:+.2f}%)")
            logging.info(f"   📊 시가: {open_price:,}원 | 고가: {high_price:,}원 | 저가: {low_price:,}원")
            logging.info(f"   📈 누적거래량: {volume:,}주")
            
        except Exception as ex:
            logging.error(f"실시간 시세 정보 표시 실패 ({code}): {ex}")
    
    def display_realtime_trade_info(self, code, data_item):
        """실시간 체결 정보를 로그에 표시"""
        try:
            # 종목명 조회
            stock_name = self.get_stock_name_by_code(code)
            
            # 체결 정보 추출
            trade_price = self.safe_int(data_item.get('prpr', 0))    # 체결가
            trade_volume = self.safe_int(data_item.get('acml_vol', 0))  # 체결량
            trade_time = data_item.get('hts_kor_isnm', '')           # 체결시간
            
            # 실시간 체결 정보 로그 출력
            logging.info(f"⚡ {stock_name} ({code}) 실시간 체결")
            logging.info(f"   💰 체결가: {trade_price:,}원 | 체결량: {trade_volume:,}주")
            if trade_time:
                logging.info(f"   ⏰ 체결시간: {trade_time}")
            
        except Exception as ex:
            logging.error(f"실시간 체결 정보 표시 실패 ({code}): {ex}")
    
    
    def update_realtime_display(self, code, data):
        """실시간 시세 표시 업데이트"""
        try:
            # 실시간 가격 정보 추출
            current_price = data.get('current_price', 0)
            change = data.get('change', 0)
            change_rate = data.get('change_rate', 0)
            volume = data.get('volume', 0)
            
            # 로그에 실시간 정보 출력
            logging.info(f"실시간 시세 [{code}]: {current_price:,}원 ({change:+,d}원, {change_rate:+.2f}%) 거래량: {volume:,}")
            
        except Exception as ex:
            logging.error(f"실시간 표시 업데이트 실패 ({code}): {ex}")
    
    def evaluate_strategy(self, code, market_data):
        """전략 평가 및 실행"""
        try:
            # 현재 전략에 따른 매수/매도 신호 평가
            strategy_name = self.current_strategy
            
            if strategy_name in self.strategy_config:
                # 매수 신호 평가
                buy_signals = self.get_buy_signals(code, market_data, strategy_name)
                if buy_signals:
                    self.execute_buy_signals(code, buy_signals)
                
                # 매도 신호 평가
                sell_signals = self.get_sell_signals(code, market_data, strategy_name)
                if sell_signals:
                    self.execute_sell_signals(code, sell_signals)
                    
        except Exception as ex:
            logging.error(f"전략 평가 실패 ({code}): {ex}")
    
    def get_buy_signals(self, code, market_data, strategy_name):
        """매수 신호 생성"""
        try:
            signals = []
            
            # 포트폴리오 상태 확인
            portfolio = self.trader.get_portfolio_status()
            if portfolio['total_holdings'] >= portfolio['max_holdings']:
                return signals
            
            # 이미 보유 중인 종목인지 확인
            if code in portfolio['holdings']:
                return signals
            
            # 전략별 매수 조건 평가 (실제로는 strategy_utils의 함수 사용)
            # 여기서는 간단한 예시만 구현
            current_price = market_data.get('current_price', 0)
            volume = market_data.get('volume', 0)
            change_rate = market_data.get('change_rate', 0)
            
            # 간단한 매수 조건 (실제로는 복잡한 전략 사용)
            if (current_price > 0 and 
                volume > 1000000 and 
                -5 < change_rate < 10):
                
                signals.append({
                    'strategy': f"{strategy_name}_buy_1",
                    'quantity': 100,  # 기본 수량
                    'price': 0,  # 시장가
                    'reason': '기본 매수 조건 충족'
                })
            
            return signals
            
        except Exception as ex:
            logging.error(f"매수 신호 생성 실패 ({code}): {ex}")
            return []
    
    def get_sell_signals(self, code, market_data, strategy_name):
        """매도 신호 생성"""
        try:
            signals = []
            
            # 보유 중인 종목인지 확인
            portfolio = self.trader.get_portfolio_status()
            if code not in portfolio['holdings']:
                return signals
            
            # 보유 정보
            buy_price = portfolio['buy_prices'].get(code, 0)
            buy_time = portfolio['buy_times'].get(code)
            current_price = market_data.get('current_price', 0)
            
            if buy_price > 0 and current_price > 0:
                profit_rate = (current_price - buy_price) / buy_price * 100
                
                # 보유 시간 계산
                hold_minutes = 0
                if buy_time:
                    hold_minutes = (datetime.now() - buy_time).total_seconds() / 60
                
                # 간단한 매도 조건
                if profit_rate >= 3.0:  # 3% 이상 수익
                    signals.append({
                        'strategy': f"{strategy_name}_sell_1",
                        'quantity': portfolio['holdings'][code].get('quantity', 100),
                        'price': 0,  # 시장가
                        'reason': f'목표 수익 달성 ({profit_rate:.2f}%)'
                    })
                elif profit_rate <= -0.7:  # 0.7% 이상 손실
                    signals.append({
                        'strategy': f"{strategy_name}_sell_2",
                        'quantity': portfolio['holdings'][code].get('quantity', 100),
                        'price': 0,  # 시장가
                        'reason': f'손절 ({profit_rate:.2f}%)'
                    })
                elif hold_minutes > 90:  # 90분 이상 보유
                    signals.append({
                        'strategy': f"{strategy_name}_sell_3",
                        'quantity': portfolio['holdings'][code].get('quantity', 100),
                        'price': 0,  # 시장가
                        'reason': f'시간 손절 ({hold_minutes:.0f}분)'
                    })
            
            return signals
            
        except Exception as ex:
            logging.error(f"매도 신호 생성 실패 ({code}): {ex}")
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
            for signal in signals:
                success = self.trader.place_sell_order(
                    code, 
                    signal['quantity'], 
                    signal['price'], 
                    signal['strategy']
                )
                
                if success:
                    self.signal_strategy_result.emit(
                        code, 
                        "sell", 
                        {
                            'strategy': signal['strategy'],
                            'reason': signal['reason'],
                            'quantity': signal['quantity'],
                            'price': signal['price']
                        }
                    )
                    
        except Exception as ex:
            logging.error(f"매도 신호 실행 실패 ({code}): {ex}")

# ==================== 자동매매 클래스 ====================
class AutoTrader(QObject):
    """자동매매 관리 클래스"""
    
    def __init__(self, trader, parent=None):
        try:
            super().__init__()           
            self.trader = trader            
            self.parent = parent            
            self.is_running = False
            logging.debug("🔍 자동매매 실행 상태 초기화 완료")
            
            # QTimer 생성을 지연시켜 메인 스레드에서 실행되도록 함
            self.auto_timer = None
            logging.debug("🔍 자동매매 타이머 변수 초기화 완료")
            
            # 메인 스레드에서 타이머 초기화 예약 (qasync 방식)
            import asyncio
            async def delayed_init_auto_timer():
                await asyncio.sleep(0.1)  # 100ms 대기
                self._initialize_auto_timer()
            asyncio.create_task(delayed_init_auto_timer())
            logging.debug("🔍 자동매매 타이머 초기화 예약 완료 (100ms 후)")
            
            logging.info("자동매매 클래스 초기화 완료")
        except Exception as ex:
            logging.error(f"❌ AutoTrader 초기화 실패: {ex}")
            import traceback
            logging.error(f"AutoTrader 초기화 예외 상세: {traceback.format_exc()}")
            raise ex
    
    def _initialize_auto_timer(self):
        """메인 스레드에서 자동매매 타이머 초기화"""
        try:            
            # QTimer 생성 및 설정
            self.auto_timer = QTimer()
            logging.debug("🔍 auto_timer timeout 시그널 연결 중...")
            self.auto_timer.timeout.connect(self.auto_trading_cycle)            
            logging.info("✅ 자동매매 타이머 초기화 완료")
        except Exception as ex:
            logging.error(f"❌ 자동매매 타이머 초기화 실패: {ex}")
            import traceback
            logging.error(f"자동매매 타이머 초기화 예외 상세: {traceback.format_exc()}")
    
    def start_auto_trading(self):
        """자동매매 시작"""
        try:
            if not self.is_running and self.auto_timer:
                self.is_running = True
                self.auto_timer.start(30000)  # 30초마다 실행
                logging.info("자동매매 시작")
            elif not self.auto_timer:
                logging.warning("자동매매 타이머가 아직 초기화되지 않았습니다")
                
        except Exception as ex:
            logging.error(f"자동매매 시작 실패: {ex}")
    
    def stop_auto_trading(self):
        """자동매매 중지"""
        try:
            if self.is_running and self.auto_timer:
                self.is_running = False
                self.auto_timer.stop()
                logging.info("자동매매 중지")
            elif not self.auto_timer:
                logging.warning("자동매매 타이머가 아직 초기화되지 않았습니다")
                
        except Exception as ex:
            logging.error(f"자동매매 중지 실패: {ex}")
    
    def auto_trading_cycle(self):
        """자동매매 사이클"""
        try:
            if not self.is_running:
                return
            
            # 시장 상태 확인
            if not self.trader.client.is_market_open():
                return
            
            # 포트폴리오 상태 업데이트
            self.trader.update_balance()
            
            # 전략 실행은 KiwoomStrategy에서 웹소켓을 통해 처리
            
        except Exception as ex:
            logging.error(f"자동매매 사이클 실패: {ex}")

# ==================== 로그인 핸들러 ====================
class LoginHandler:
    """로그인 처리 클래스"""
    
    def __init__(self, parent_window):
        self.parent = parent_window
        self.config = configparser.RawConfigParser()
        self.kiwoom_client = None
    
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
            import aiofiles
            import asyncio
            
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
        import io
        string_io = io.StringIO()
        self.config.write(string_io)
        return string_io.getvalue()
    
    async def start_websocket_client(self):
        """웹소켓 클라이언트 시작 (qasync 방식)"""
        try:           
            # 웹소켓 클라이언트 초기화
            token = self.kiwoom_client.access_token
            is_mock = self.kiwoom_client.is_mock
            logger = logging.getLogger('KiwoomWebSocketClient')
            
            logging.info("🔧 웹소켓 클라이언트 초기화 시작...")
            self.websocket_client = KiwoomWebSocketClient(token, logger, is_mock, self.parent)
            
            # 웹소켓 서버에 먼저 연결한 후 실행 (메인 스레드에서 qasync 사용)
            logging.info("🔧 웹소켓 서버 연결 시도...")
            
            # 메인 스레드에서 qasync로 웹소켓 실행
            import asyncio
            
            # 웹소켓 클라이언트를 비동기 태스크로 실행
            self.websocket_task = asyncio.create_task(self.websocket_client.run())
            
            logging.info("✅ 웹소켓 클라이언트 시작 완료 (메인 스레드에서 qasync 실행)")
            
        except Exception as e:
            logging.error(f"❌ 웹소켓 클라이언트 시작 실패: {e}")
            import traceback
            logging.error(f"웹소켓 클라이언트 시작 에러 상세: {traceback.format_exc()}")
    
    def handle_api_connection(self):
        """키움 REST API 연결 처리"""
        try:
            # 설정 저장 (동기 방식으로 안전하게 실행)
            try:
                self.save_settings_sync()
            except Exception as ex:
                logging.error(f"설정 저장 실패: {ex}")
            
            # 키움 REST API 연결
            self.kiwoom_client = init_kiwoom_client()
            
            if self.kiwoom_client and self.kiwoom_client.is_connected:
                # 연결 상태 업데이트
                self.parent.connectionStatusLabel.setText("연결 상태: 연결됨")
                self.parent.connectionStatusLabel.setProperty("class", "connected")
                
                # 거래 모드에 따른 메시지
                mode = "모의투자" if self.parent.tradingModeCombo.currentIndex() == 0 else "실제투자"
                logging.info(f"키움 REST API 연결 성공! 거래 모드: {mode}")
                
            else:
                logging.error("키움 REST API 연결 실패! settings.ini 파일의 appkey와 appsecret을 확인해주세요.")
                
        except Exception as ex:
            logging.error(f"API 연결 처리 실패: {ex}")
    
    def buycount_setting(self):
        """투자 종목수 설정"""
        try:
            buycount = int(self.parent.buycountEdit.text())
            if buycount > 0:
                logging.info(f"최대 투자 종목수 설정: {buycount}")
            else:
                logging.warning("1 이상의 숫자를 입력해주세요.")
        except ValueError:
            logging.warning("올바른 숫자를 입력해주세요.")
        except Exception as ex:
            logging.error(f"투자 종목수 설정 실패: {ex}")

# ==================== 메인 윈도우 ====================
class MyWindow(QWidget):
    """메인 윈도우 클래스"""
    
    def __init__(self, webengine_available=False):
        super().__init__()
        
        # 기본 변수 초기화
        self.is_loading_strategy = False
        self.market_close_emitted = False
        self.webengine_available = webengine_available  # WebEngine 사용 가능 여부
        
        # 객체 초기화
        self.trader = None
        self.objstg = None
        self.autotrader = None
        self.kiwoom_client = None
        self.chart_cache = None  # 차트 데이터 캐시
        
        # 조건검색 관련 변수
        self.condition_list = []  # 조건검색 목록
        self.active_realtime_conditions = set()  # 활성화된 실시간 조건검색
        self.condition_search_results = {}  # 조건검색 결과 저장
        self.chart_drawing_lock = Lock()
        
        # UI 생성
        self.init_ui()

        # 로그인 핸들러 생성
        self.login_handler = LoginHandler(self)
        self.login_handler.load_settings_sync()

        # 자동 연결 시도 (qasync 방식)
        import asyncio
        asyncio.create_task(self.attempt_auto_connect())
        
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
        self.setWindowTitle("키움 REST API 자동매매 프로그램 v3.0")
        self.setGeometry(0, 0, 1900, 980)
        
        # 전체 애플리케이션 스타일 적용
        self.apply_modern_style()
        
        # ===== 메인 탭 위젯 생성 =====
        self.tab_widget = QTabWidget()
        
        # 탭 1: 실시간 매매
        self.trading_tab = QWidget()
        self.init_trading_tab()
        self.tab_widget.addTab(self.trading_tab, "실시간 매매")
        
        # 탭 2: 백테스팅
        self.backtest_tab = QWidget()
        self.init_backtest_tab()
        self.tab_widget.addTab(self.backtest_tab, "백테스팅")
        
        # 메인 레이아웃
        main_layout = QVBoxLayout()
        main_layout.addWidget(self.tab_widget)
        self.setLayout(main_layout)
    
    def _create_placeholder_widget(self):
        """차트 브라우저 초기화 전 임시 위젯 생성"""
        try:
            from PyQt6.QtWidgets import QLabel
            placeholder = QLabel("📊 차트 영역 초기화 중...")
            placeholder.setStyleSheet("""
                QLabel {
                    background-color: #f0f0f0;
                    border: 1px solid #ccc;
                    font-size: 14px;
                    color: #666;
                    padding: 50px;
                    text-align: center;
                }
            """)
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            return placeholder
        except Exception as ex:
            logging.error(f"플레이스홀더 위젯 생성 실패: {ex}")
            return QLabel("차트 영역")
    
    def init_trading_tab(self):
        """실시간 매매 탭 초기화"""
        
        # ===== 키움 REST API 연결 영역 =====
        loginLayout = QVBoxLayout()

        # API 연결 상태 표시
        statusLayout = QHBoxLayout()
        self.connectionStatusLabel = QLabel("연결 상태: 미연결")
        self.connectionStatusLabel.setProperty("class", "disconnected")
        statusLayout.addWidget(self.connectionStatusLabel)
        statusLayout.addStretch()
        
        # 모의투자/실제투자 구분
        tradingModeLayout = QHBoxLayout()
        
        self.tradingModeCombo = QComboBox()
        self.tradingModeCombo.addItem("모의투자")
        self.tradingModeCombo.addItem("실제투자")
        self.tradingModeCombo.setFixedWidth(120)
        tradingModeLayout.addWidget(self.tradingModeCombo)
        
        # 자동 연결 설정
        self.autoConnectCheckBox = QCheckBox("자동 연결")
        tradingModeLayout.addWidget(self.autoConnectCheckBox)

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
        self.buycountEdit = QLineEdit("3")
        buycountLayout.addWidget(self.buycountEdit)
        self.buycountButton = QPushButton("설정")
        self.buycountButton.setFixedWidth(70)
        buycountLayout.addWidget(self.buycountButton)
        
        # 구분선 추가
        separator2 = QFrame()
        separator2.setProperty("class", "separator")
        buycountLayout.addWidget(separator2)

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
        logging.info("📋 monitoringBox 생성 완료")
        
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
        logging.info("📋 boughtBox 생성 완료")
        secondButtonLayout = QHBoxLayout()
        self.sellButton = QPushButton("매도")
        self.sellButton.setProperty("class", "danger")
        secondButtonLayout.addWidget(self.sellButton)
        self.sellAllButton = QPushButton("전부 매도")
        self.sellAllButton.setProperty("class", "danger")
        secondButtonLayout.addWidget(self.sellAllButton)     
        boughtBoxLayout.addLayout(secondButtonLayout)

        # ===== 출력 버튼 =====
        printLayout = QHBoxLayout()
        self.printChartButton = QPushButton("차트 출력")
        printLayout.addWidget(self.printChartButton)
        self.dataOutputButton2 = QPushButton("차트데이터 저장")
        printLayout.addWidget(self.dataOutputButton2)

        # ===== 왼쪽 영역 통합 =====
        listBoxesLayout = QVBoxLayout()
        listBoxesLayout.addLayout(loginLayout)
        listBoxesLayout.addLayout(buycountLayout)
        listBoxesLayout.addLayout(monitoringBoxLayout, 6)
        listBoxesLayout.addLayout(boughtBoxLayout, 4)
        listBoxesLayout.addLayout(printLayout)

        # ===== 차트 영역 (Plotly 기반) =====
        chartLayout = QVBoxLayout()

        # 차트 브라우저를 즉시 초기화하여 UI 깜빡임 방지
        self.chart_browser = None
        self.chart_layout = chartLayout  # 차트 레이아웃 참조 저장
        
        # 지연 초기화 대신 즉시 초기화
        self._safe_initialize_chart_browser(chartLayout)

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

        # 주식 현황 테이블
        self.stock_table = QTableWidget()
        self.stock_table.setRowCount(0)
        self.stock_table.setColumnCount(6)
        self.stock_table.setHorizontalHeaderLabels(["종목코드", "현재가", "상승확률(%)", "매수가", "평가손익", "수익률(%)"])
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
        self.trading_tab.setLayout(mainLayout)

        # ===== 전략 콤보박스 초기화 =====
        self.load_strategy_combos()
        
        # ===== 차트 드로어 초기화 (Plotly 기반) - 지연 초기화 =====
        self.chartdrawer = None
        self.chart_init_retry_count = 0
        self.max_chart_init_retries = 3
        
        # ===== 이벤트 연결 =====
        self.tradingModeCombo.currentIndexChanged.connect(self.trading_mode_changed)
        self.buycountButton.clicked.connect(self.buycount_setting)
        self.addStockButton.clicked.connect(self.add_stock_to_list)
        self.stockInputEdit.returnPressed.connect(self.add_stock_to_list)

        self.buyButton.clicked.connect(self.buy_item)
        self.deleteFirstButton.clicked.connect(self.delete_select_item)
        self.sellButton.clicked.connect(self.sell_item)
        self.sellAllButton.clicked.connect(self.sell_all_item)

        # 리스트박스 이벤트 연결
        logging.info("🔗 리스트박스 이벤트 연결 시작...")
        self.monitoringBox.itemClicked.connect(self.listBoxChanged)
        self.boughtBox.itemClicked.connect(self.listBoxChanged)
        logging.info("✅ 리스트박스 클릭 이벤트 연결 완료")
        
        # 리스트박스 활성화
        self.monitoringBox.setEnabled(True)
        self.boughtBox.setEnabled(True)
        logging.info("✅ 리스트박스 활성화 완료")
        

        self.printChartButton.clicked.connect(self.print_chart)
        self.dataOutputButton2.clicked.connect(self.output_current_data)

        self.comboStg.currentIndexChanged.connect(self.stgChanged)
        self.comboBuyStg.currentIndexChanged.connect(self.buyStgChanged)
        self.comboSellStg.currentIndexChanged.connect(self.sellStgChanged)
        self.saveBuyStgButton.clicked.connect(self.save_buystrategy)
        self.saveSellStgButton.clicked.connect(self.save_sellstrategy)
    
    def init_backtest_tab(self):
        """백테스팅 탭 초기화"""
        
        layout = QVBoxLayout()
        
        # ===== 설정 영역 =====
        settings_group = QGroupBox("백테스팅 설정")
        settings_layout = QGridLayout()
        
        # 기간 선택
        settings_layout.addWidget(QLabel("시작일:"), 0, 0)
        self.bt_start_date = QLineEdit()
        self.bt_start_date.setPlaceholderText("YYYYMMDD (예: 20250101)")
        self.bt_start_date.setFixedWidth(150)
        settings_layout.addWidget(self.bt_start_date, 0, 1)
        
        settings_layout.addWidget(QLabel("종료일:"), 0, 2)
        self.bt_end_date = QLineEdit()
        self.bt_end_date.setPlaceholderText("YYYYMMDD (예: 20250131)")
        self.bt_end_date.setFixedWidth(150)
        settings_layout.addWidget(self.bt_end_date, 0, 3)
        
        # DB 기간 불러오기 버튼
        self.bt_load_period_button = QPushButton("DB 기간 불러오기")
        self.bt_load_period_button.setFixedWidth(130)
        self.bt_load_period_button.clicked.connect(self.load_db_period)
        settings_layout.addWidget(self.bt_load_period_button, 0, 4)
        
        # 초기 자금
        settings_layout.addWidget(QLabel("초기 자금:"), 1, 0)
        self.bt_initial_cash = QLineEdit("10000000")
        self.bt_initial_cash.setFixedWidth(150)
        settings_layout.addWidget(self.bt_initial_cash, 1, 1)
        
        # 전략 선택
        settings_layout.addWidget(QLabel("투자 전략:"), 2, 0)
        self.bt_strategy_combo = QComboBox()
        self.bt_strategy_combo.setFixedWidth(150)
        settings_layout.addWidget(self.bt_strategy_combo, 2, 1)
        
        # 백테스팅 전략 콤보박스 로드
        self.load_backtest_strategies()
        
        # 실행 버튼
        self.bt_run_button = QPushButton("백테스팅 실행")
        self.bt_run_button.setFixedWidth(150)
        self.bt_run_button.clicked.connect(self.run_backtest)
        settings_layout.addWidget(self.bt_run_button, 2, 2)
        
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
        
        # 백테스팅 차트는 현재 비활성화
        # self.bt_fig = Figure(figsize=(10, 8))
        # self.bt_canvas = FigureCanvas(self.bt_fig)
        # right_layout.addWidget(self.bt_canvas)
        
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
        
        # 백테스팅 일봉 차트는 현재 비활성화
        # self.bt_daily_fig = Figure(figsize=(10, 8))
        # self.bt_daily_canvas = FigureCanvas(self.bt_daily_fig)
        # daily_right_layout.addWidget(self.bt_daily_canvas)
        
        daily_right_widget.setLayout(daily_right_layout)
        
        daily_layout.addWidget(daily_left_widget, 1)
        daily_layout.addWidget(daily_right_widget, 2)
        daily_tab.setLayout(daily_layout)
        
        # 탭 추가
        results_tab_widget.addTab(overall_tab, "전체 성과")
        results_tab_widget.addTab(daily_tab, "일별 성과")
        
        layout.addWidget(results_tab_widget)
        
        self.backtest_tab.setLayout(layout)
        
        # 초기화 시 DB 기간 자동 로드 (qasync 방식)
        import asyncio
        async def delayed_load_db():
            await asyncio.sleep(0.1)  # 100ms 대기
            self.load_db_period()
        asyncio.create_task(delayed_load_db())
    
    async def attempt_auto_connect(self):
        """자동 연결 시도"""
        try:
            if self.login_handler.config.getboolean('LOGIN', 'autoconnect', fallback=False):
                self.login_handler.handle_api_connection()
                await self.login_handler.start_websocket_client()
                # post_login_setup()은 웹소켓 로그인 성공 시 자동으로 호출됨
                
        except Exception as ex:
            logging.error(f"자동 연결 시도 실패: {ex}")
    
    
    def safe_int(self, value, default=0):
        """안전한 정수 변환 함수"""
        try:
            if value is None or value == '' or value == '-':
                return default
            return int(str(value).replace(',', ''))
        except (ValueError, TypeError):
            return default
    
    def safe_float(self, value, default=0.0):
        """안전한 실수 변환 함수"""
        try:
            if value is None or value == '' or value == '-':
                return default
            return float(str(value).replace(',', ''))
        except (ValueError, TypeError):
            return default
    
    async def handle_condition_search_list_query(self):
        """조건검색 목록조회 (웹소켓 기반)"""
        try:
            logging.info("🔍 조건검색 목록조회 시작 (웹소켓)")
            
            if not hasattr(self, 'trader') or not self.trader:
                logging.warning("⚠️ 트레이더가 초기화되지 않았습니다")
                return
            
            if not hasattr(self.trader, 'client') or not self.trader.client:
                logging.warning("⚠️ API 클라이언트가 연결되지 않았습니다")
                return
            
            # 웹소켓 클라이언트 확인
            if not hasattr(self.login_handler, 'websocket_client') or not self.login_handler.websocket_client:
                logging.warning("⚠️ 웹소켓 클라이언트가 연결되지 않았습니다")
                return
            
            # 웹소켓을 통한 조건검색 목록조회
            try:
                await self.login_handler.websocket_client.send_message({ 
                    'trnm': 'CNSRLST', # TR명
                })
                logging.info("✅ 조건검색 목록조회 요청 전송 완료 (웹소켓)")
                
                # 웹소켓 응답은 receive_messages에서 처리됨
                logging.info("💾 조건검색 목록조회 요청 완료 - 응답은 웹소켓에서 처리됩니다")
                    
            except Exception as websocket_ex:
                logging.error(f"❌ 조건검색 목록조회 웹소켓 요청 실패: {websocket_ex}")
                import traceback
                logging.error(f"웹소켓 요청 예외 상세: {traceback.format_exc()}")
                self.condition_search_list = None
                
        except Exception as ex:
            logging.error(f"❌ 조건검색 목록조회 실패: {ex}")
            import traceback
            logging.error(f"조건검색 목록조회 예외 상세: {traceback.format_exc()}")
            self.condition_search_list = None
    
    def handle_acnt_balance_query(self):
        """계좌 잔고조회 및 기본정보 조회 통합 처리 - 강화된 예외 처리"""
        try:
            logging.info("🔧 계좌 잔고조회 시작")
            logging.info(f"현재 스레드: {threading.current_thread().name}")
            logging.info(f"메인 스레드 여부: {threading.current_thread() is threading.main_thread()}")
            
            if not hasattr(self, 'trader') or not self.trader:
                logging.warning("⚠️ 트레이더가 초기화되지 않았습니다")
                return
            
            if not hasattr(self.trader, 'client') or not self.trader.client:
                logging.warning("⚠️ API 클라이언트가 연결되지 않았습니다")
                return
            
            logging.info("=" * 60)
            logging.info("📊 계좌 기본정보 및 잔고조회 시작")
            logging.info("=" * 60)
            logging.info("🔍 계좌 잔고 조회 중...")
            
            # REST API 잔고조회 시도
            try:
                balance_data = self.trader.client.get_acnt_balance()
                if balance_data:
                    logging.info("✅ 계좌 잔고 조회 성공")
                    
                    # 계좌 기본정보 표시
                    if 'output2' in balance_data and balance_data['output2']:
                        account_info = balance_data['output2'][0]  # 첫 번째 계좌 정보
                        
                        logging.info("📋 계좌 기본정보:")
                        logging.info(f"  💰 예수금총금액: {self.safe_int(account_info.get('dnca_tot_amt', '0')):,}원")
                        logging.info(f"  💵 익일정산금액: {self.safe_int(account_info.get('nxdy_excc_amt', '0')):,}원")
                        logging.info(f"  🏦 가수도정산금액: {self.safe_int(account_info.get('prvs_rcdl_excc_amt', '0')):,}원")
                        logging.info(f"  📈 CMA평가금액: {self.safe_int(account_info.get('cma_evlu_amt', '0')):,}원")
                        logging.info(f"  💎 유가평가금액: {self.safe_int(account_info.get('scts_evlu_amt', '0')):,}원")
                        logging.info(f"  📊 총평가금액: {self.safe_int(account_info.get('tot_evlu_amt', '0')):,}원")
                        logging.info(f"  🎯 순자산금액: {self.safe_int(account_info.get('nass_amt', '0')):,}원")
                        logging.info(f"  📉 전일총자산평가금액: {self.safe_int(account_info.get('bfdy_tot_asst_evlu_amt', '0')):,}원")
                        logging.info(f"  📈 자산증감액: {self.safe_int(account_info.get('asst_icdc_amt', '0')):,}원")
                        
                        # 자산증감수익률 계산
                        asset_change_rate = self.safe_float(account_info.get('asst_icdc_erng_rt', '0'))
                        if asset_change_rate != 0:
                            change_symbol = "📈" if asset_change_rate > 0 else "📉"
                            logging.info(f"  {change_symbol} 자산증감수익률: {asset_change_rate:.2f}%")
                        else:
                            logging.info(f"  📊 자산증감수익률: 0.00%")
                    
                    # 보유 종목 정보 표시
                    if 'output1' in balance_data and balance_data['output1']:
                        holdings = balance_data['output1']
                        logging.info(f"📦 보유 종목 수: {len(holdings)}개")
                        
                        if len(holdings) > 0:
                            logging.info("📋 보유 종목 상세:")
                            total_profit_loss = 0
                            total_investment = 0
                            
                            for i, stock in enumerate(holdings[:10], 1):  # 최대 10개만 표시
                                stock_name = stock.get('prdt_name', '알 수 없음')
                                stock_code = stock.get('pdno', '알 수 없음')
                                quantity = self.safe_int(stock.get('hldg_qty', 0))
                                current_price = self.safe_int(stock.get('prpr', 0))
                                avg_price = self.safe_int(stock.get('pchs_avg_pric', 0))
                                profit_loss = self.safe_int(stock.get('evlu_pfls_amt', 0))
                                profit_rate = self.safe_float(stock.get('evlu_pfls_rt', 0))
                                
                                if quantity > 0:  # 보유수량이 있는 경우만 표시
                                    current_value = quantity * current_price
                                    investment_value = quantity * avg_price
                                    
                                    logging.info(f"  {i:2d}. {stock_name} ({stock_code})")
                                    logging.info(f"      보유수량: {quantity:,}주 | 현재가: {current_price:,}원 | 매입단가: {avg_price:,}원")
                                    logging.info(f"      평가금액: {current_value:,}원 | 매입금액: {investment_value:,}원")
                                    
                                    if profit_loss != 0:
                                        profit_symbol = "📈" if profit_loss > 0 else "📉"
                                        logging.info(f"      {profit_symbol} 평가손익: {profit_loss:,}원 ({profit_rate:+.2f}%)")
                                    else:
                                        logging.info(f"      📊 평가손익: 0원 (0.00%)")
                                    
                                    total_profit_loss += profit_loss
                                    total_investment += investment_value
                            
                            if len(holdings) > 10:
                                logging.info(f"  ... 외 {len(holdings) - 10}개 종목")
                            
                            logging.info(f"📊 전체 보유종목 평가손익: {total_profit_loss:,}원")
                            
                            # 보유종목에 대한 실시간 구독 실행
                            holding_codes = [stock.get('pdno', '') for stock in holdings if stock.get('pdno')]
                            if holding_codes:
                                self.subscribe_holdings_realtime(holding_codes)
                        else:
                            logging.info("📦 보유 종목이 없습니다.")
                    else:
                        logging.info("📦 보유 종목이 없습니다.")
                    
                    # 보유종목 리스트에 추가
                    self.add_acnt_balance_stocks_to_list(balance_data)
                    
                    logging.info("=" * 60)
                    logging.info("✅ 계좌 기본정보 및 잔고조회 완료")
                    logging.info("=" * 60)
                    
                else:
                    logging.warning("⚠️ 계좌 잔고 조회 실패 - 계좌정보를 가져올 수 없습니다")
                    
            except Exception as balance_ex:
                logging.error(f"❌ 계좌 잔고 조회 실패: {balance_ex}")
                import traceback
                logging.error(f"잔고 조회 예외 상세: {traceback.format_exc()}")
                logging.info("⚠️ 잔고 조회 실패했지만 프로그램을 계속 실행합니다")
                
        except Exception as ex:
            logging.error(f"❌ 계좌 기본정보 및 잔고조회 실패: {ex}")
            import traceback
            logging.error(f"계좌 조회 예외 상세: {traceback.format_exc()}")
            logging.info("⚠️ 계좌 조회 실패했지만 프로그램을 계속 실행합니다")
    
    def subscribe_holdings_realtime(self, holding_codes):
        """보유종목에 대한 실시간 구독 실행 (중단됨)"""
        try:
            # 실시간 구독 요청 중단
            logging.info(f"⏸️ 보유종목 실시간 구독 중단: {holding_codes}")
            
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
            #             logging.info(f"🔄 보유종목 실시간 구독 실행: {holding_codes}")
            #         else:
            #             logging.info(f"보유종목 구독 변경 없음, 업데이트 건너뜀: {holding_codes}")
        except Exception as ex:
            logging.error(f"❌ 보유종목 실시간 구독 실패: {ex}")
            import traceback
            logging.error(f"보유종목 구독 예외 상세: {traceback.format_exc()}")
            logging.info("⚠️ 보유종목 구독 실패했지만 프로그램을 계속 실행합니다")
    
    def extract_monitoring_stock_codes(self):
        """모니터링 종목 코드 추출 및 로그 출력 - 강화된 예외 처리"""
        try:
            logging.info("🔧 모니터링 종목 코드 추출 시작")
            logging.info(f"현재 스레드: {threading.current_thread().name}")
            logging.info(f"메인 스레드 여부: {threading.current_thread() is threading.main_thread()}")
            logging.info("=" * 50)
            logging.info("📋 모니터링 종목 코드 추출 시작")
            logging.info("=" * 50)
            
            # 모니터링 종목 코드 추출
            monitoring_codes = self.get_monitoring_stock_codes()
            logging.info(f"모니터링 종목 코드 추출: {monitoring_codes}")
            logging.info(f"📋 모니터링 종목: {monitoring_codes}")
            
            logging.info("=" * 50)
            logging.info("✅ 모니터링 종목 코드 추출 완료")
            logging.info("=" * 50)
            
            # 모니터링 종목 코드 추출 완료 후 차트 캐시 업데이트
            logging.info(f"📋 모니터링 종목 코드 추출 완료: {monitoring_codes}")
            
            # 주식체결 실시간 구독 추가
            try:
                if hasattr(self, 'login_handler') and hasattr(self.login_handler, 'kiwoom_client'):
                    # 웹소켓 클라이언트 참조가 제거되어 주식체결 구독 기능 비활성화
                    # 주식체결 구독은 별도로 관리되어야 함
                    logging.info(f"주식체결 구독 기능은 별도로 관리됩니다: {monitoring_codes}")
                else:
                    logging.warning("⚠️ 키움 클라이언트가 초기화되지 않았습니다")
            except Exception as exec_sub_ex:
                logging.error(f"❌ 주식체결 구독 실패: {exec_sub_ex}")
                import traceback
                logging.error(f"주식체결 구독 예외 상세: {traceback.format_exc()}")
            
            # 차트 데이터 캐시 업데이트 (중요!)
            try:
                if hasattr(self, 'chart_cache') and self.chart_cache:
                    logging.info(f"🔧 차트 캐시 업데이트 시작: {monitoring_codes}")
                    self.chart_cache.update_monitoring_stocks(monitoring_codes)
                    logging.info("✅ 차트 캐시 업데이트 완료")
                else:
                    logging.warning("⚠️ 차트 캐시가 초기화되지 않았습니다")
            except Exception as cache_ex:
                logging.error(f"❌ 차트 캐시 업데이트 실패: {cache_ex}")
                import traceback
                logging.error(f"차트 캐시 업데이트 예외 상세: {traceback.format_exc()}")
            
            return monitoring_codes
                
        except Exception as ex:
            logging.error(f"❌ 모니터링 종목 코드 추출 실패: {ex}")
            import traceback
            logging.error(f"모니터링 종목 추출 예외 상세: {traceback.format_exc()}")
            logging.info("⚠️ 모니터링 종목 추출 실패했지만 기본값으로 계속 실행합니다")
            return [
                '005930', '005380', '000660', '035420', '207940', '006400', 
                '051910', '035720', '068270', '323410', '000270'
            ]  # 기본값으로 주요 종목들 반환
    
    
    
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
            for i in range(self.boughtBox.count()):
                existing_items.append(self.boughtBox.item(i).text())
            
            # "종목코드 - 종목명" 형식으로 표시 (기존 형식과 일치)
            stock_display = f"{stock_code} - {stock_name}"
            
            # 중복되지 않는 경우만 보유종목 리스트에 추가
            if stock_display not in existing_items:
                self.boughtBox.addItem(stock_display)
                logging.info(f"✅ 실시간 잔고 종목을 보유종목에 자동 추가: {stock_display} ({quantity}주)")
            else:
                logging.debug(f"이미 보유종목에 존재: {stock_display}")
                
        except Exception as ex:
            logging.error(f"실시간 잔고 종목 추가 실패: {ex}")
            import traceback
            logging.error(f"실시간 잔고 종목 추가 에러 상세: {traceback.format_exc()}")
    
    
    def display_acnt_balance_info(self, balance_data, cash_data):
        """계좌평가현황 정보 표시"""
        try:
            logging.info("=== 계좌평가현황 정보 ===")
            
            # 계좌평가현황 정보 표시
            if balance_data:
                logging.info("=== 계좌 정보 ===")
                
                # 계좌 정보 표시
                if 'data' in balance_data:
                    data = balance_data['data']
                    
                    # 계좌 기본 정보
                    account_info = data.get('account_info', {})
                    if account_info:
                        logging.info(f"계좌번호: {account_info.get('account_no', 'N/A')}")
                        logging.info(f"계좌명: {account_info.get('account_name', 'N/A')}")
                        logging.info(f"계좌상태: {account_info.get('account_status', 'N/A')}")
                        logging.info(f"계좌유형: {account_info.get('account_type', 'N/A')}")
                        logging.info(f"거래소: {account_info.get('exchange', 'N/A')}")
                        logging.info("")
                    else:
                        logging.info("계좌 기본 정보를 찾을 수 없습니다")
                    
                    # 총 자산 정보
                    total_info = data.get('total_info', {})
                    if total_info:
                        total_asset = total_info.get('total_asset', 0)
                        total_profit_loss = total_info.get('total_profit_loss', 0)
                        total_profit_rate = total_info.get('total_profit_rate', 0)
                        total_investment = total_info.get('total_investment', 0)
                        
                        logging.info("=== 자산 현황 ===")
                        logging.info(f"총 자산: {total_asset:,}원")
                        logging.info(f"총 투자금액: {total_investment:,}원")
                        logging.info(f"총 평가손익: {total_profit_loss:+,}원")
                        logging.info(f"총 수익률: {total_profit_rate:+.2f}%")
                        logging.info("")
                    else:
                        logging.info("총 자산 정보를 찾을 수 없습니다")
                    
                    # 현금 정보
                    cash_info = data.get('cash_info', {})
                    if cash_info:
                        total_cash = cash_info.get('total_cash', 0)
                        available_cash = cash_info.get('available_cash', 0)
                        deposit = cash_info.get('deposit', 0)
                        loan = cash_info.get('loan', 0)
                        
                        logging.info("=== 현금 정보 ===")
                        logging.info(f"총 현금: {total_cash:,}원")
                        logging.info(f"가용 현금: {available_cash:,}원")
                        logging.info(f"예수금: {deposit:,}원")
                        logging.info(f"대출금: {loan:,}원")
                        logging.info("")
                    else:
                        logging.info("현금 정보를 찾을 수 없습니다")
                else:
                    logging.info("계좌평가현황 데이터를 찾을 수 없습니다")
            
            
            # 보유 종목 정보 표시 (추출된 데이터 사용)
            holdings = self.extract_holdings_from_acnt_balance(balance_data) if balance_data else []
            if holdings:
                logging.info(f"보유 종목 수: {len(holdings)}개")
                for holding in holdings[:5]:  # 최대 5개만 표시
                    code = holding.get('code', 'N/A')
                    name = holding.get('name', 'N/A')
                    quantity = holding.get('quantity', 0)
                    avg_price = holding.get('avg_price', 0)
                    current_price = holding.get('current_price', 0)
                    profit_loss = holding.get('profit_loss', 0)
                    profit_rate = holding.get('profit_rate', 0)
                    
                    logging.info(f"  [{code}] {name}: {quantity}주, 평균단가: {avg_price:,}원, 현재가: {current_price:,}원, 손익: {profit_loss:+,d}원 ({profit_rate:+.2f}%)")
            else:
                logging.info("보유 종목이 없습니다")
            
            logging.info("=== 계좌평가현황 조회 완료 ===")
                    
        except Exception as ex:
            logging.error(f"계좌평가현황 정보 표시 실패: {ex}")
    
    def add_acnt_balance_stocks_to_list(self, balance_data):
        """계좌평가현황에서 보유종목을 보유종목 리스트에 추가"""
        try:
            if not balance_data:
                logging.info("잔고 데이터가 없습니다")
                return
            
            # 계좌평가현황 응답에서 보유종목 정보 추출
            holdings = self.extract_holdings_from_acnt_balance(balance_data)
            if not holdings:
                logging.info("보유 종목이 없어 보유종목 리스트에 추가할 항목이 없습니다")
                return
            
            added_count = 0
            for holding in holdings:
                code = holding.get('code', '')
                name = holding.get('name', '')
                quantity = holding.get('quantity', 0)
                
                if code and name and quantity > 0:
                    # 종목명과 종목코드를 결합한 문자열 생성
                    stock_display = f"{name} ({code}) - {quantity}주"
                    
                    # 이미 보유종목 리스트에 있는지 확인
                    existing_items = []
                    for i in range(self.boughtBox.count()):
                        existing_items.append(self.boughtBox.item(i).text())
                    
                    # 중복되지 않는 경우만 보유종목 리스트에 추가
                    if stock_display not in existing_items:
                        self.boughtBox.addItem(stock_display)
                        added_count += 1
                        logging.info(f"보유 종목을 보유종목 리스트에 추가: {stock_display}")
            
            if added_count > 0:
                logging.info(f"총 {added_count}개 보유 종목이 보유종목 리스트에 추가되었습니다")
            else:
                logging.info("추가할 새로운 보유 종목이 없습니다 (모든 종목이 이미 보유종목 리스트에 있음)")
                
        except Exception as ex:
            logging.error(f"보유 종목 리스트 추가 실패: {ex}")
    
    def extract_holdings_from_acnt_balance(self, balance_data):
        """계좌평가현황 응답에서 보유종목 정보 추출"""
        try:
            holdings = []
            
            # 키움 API 응답 구조에 따라 보유종목 정보 추출
            # 실제 응답 구조를 확인하여 적절한 필드명 사용
            if 'data' in balance_data:
                data = balance_data['data']
                
                # 가능한 필드명들 시도
                possible_fields = [
                    'holdings',
                    'stock_list', 
                    'stock_info',
                    'balance_list',
                    'account_balance',
                    'stock_balance'
                ]
                
                for field in possible_fields:
                    if field in data:
                        holdings_data = data[field]
                        if isinstance(holdings_data, list):
                            for item in holdings_data:
                                # 종목 정보 추출 (필드명이 다를 수 있음)
                                code = item.get('stock_code') or item.get('code') or item.get('stk_cd', '')
                                name = item.get('stock_name') or item.get('name') or item.get('stk_nm', '')
                                quantity = int(item.get('quantity') or item.get('qty') or item.get('hldg_qty', 0))
                                
                                if code and name and quantity > 0:
                                    holdings.append({
                                        'code': code,
                                        'name': name,
                                        'quantity': quantity,
                                        'avg_price': int(item.get('avg_price') or item.get('avg_prc') or item.get('pchs_avg_prc', 0)),
                                        'current_price': int(item.get('current_price') or item.get('cur_prc') or item.get('prc', 0)),
                                        'profit_loss': int(item.get('profit_loss') or item.get('pl') or item.get('evlu_pfls_amt', 0)),
                                        'profit_rate': float(item.get('profit_rate') or item.get('pl_rate') or item.get('evlu_pfls_rt', 0))
                                    })
                            break
            
            logging.debug(f"추출된 보유종목 수: {len(holdings)}")
            return holdings
            
        except Exception as e:
            logging.error(f"보유종목 정보 추출 실패: {e}")
            return []
    
    def trading_mode_changed(self):
        """거래 모드 변경"""
        try:
            mode = "모의투자" if self.tradingModeCombo.currentIndex() == 0 else "실제투자"
            logging.info(f"거래 모드 변경: {mode}")
            
            # 연결된 상태라면 재연결 안내 (로그로만 표시)
            if hasattr(self, 'trader') and self.trader and self.trader.client and self.trader.client.is_connected:
                logging.info(f"거래 모드가 {mode}로 변경되었습니다. 새로운 설정을 적용하려면 API를 재연결해주세요.")
                
        except Exception as ex:
            logging.error(f"거래 모드 변경 실패: {ex}")
    
    def load_strategy_combos(self):
        """전략 콤보박스에 settings.ini 값 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # 투자전략 콤보박스 로드
            self.comboStg.clear()
            if config.has_section('STRATEGIES'):
                for key, value in config.items('STRATEGIES'):
                    if key.startswith('stg_') or key == 'stg_integrated':
                        self.comboStg.addItem(value)
            
            # 기본 전략 설정
            if config.has_option('SETTINGS', 'last_strategy'):
                last_strategy = config.get('SETTINGS', 'last_strategy')
                index = self.comboStg.findText(last_strategy)
                if index >= 0:
                    self.comboStg.setCurrentIndex(index)
                    logging.info(f"✅ 저장된 투자전략 복원: {last_strategy}")
                    
                    # 조건검색식인 경우는 조건검색 목록 로드 후 자동 실행됨
                    if last_strategy.startswith("[조건검색]"):
                        logging.info("🔍 저장된 조건검색식 발견 - 조건검색 목록 로드 후 자동 실행 예정")
                else:
                    logging.warning(f"⚠️ 저장된 투자전략을 찾을 수 없습니다: {last_strategy}")
            else:
                logging.info("저장된 투자전략이 없습니다. 기본 전략을 사용합니다.")
            
            # 매수전략 콤보박스 로드 (첫 번째 투자전략의 매수전략들)
            self.load_buy_strategies()
            
            # 매도전략 콤보박스 로드 (첫 번째 투자전략의 매도전략들)
            self.load_sell_strategies()
            
            # 초기 전략 내용 로드
            self.load_initial_strategy_content()
            
            logging.info("투자전략 콤보박스 로드 완료")
            
        except Exception as ex:
            logging.error(f"전략 콤보박스 로드 실패: {ex}")
    
    def load_buy_strategies(self):
        """매수전략 콤보박스 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            self.comboBuyStg.clear()
            current_strategy = self.comboStg.currentText()
            
            if config.has_section(current_strategy):
                buy_strategies = []
                for key, value in config.items(current_strategy):
                    if key.startswith('buy_stg_'):
                        try:
                            strategy_data = eval(value)  # JSON 파싱
                            if isinstance(strategy_data, dict) and 'name' in strategy_data:
                                buy_strategies.append(strategy_data['name'])
                        except:
                            continue
                
                for strategy_name in buy_strategies:
                    self.comboBuyStg.addItem(strategy_name)
                
                if buy_strategies:
                    self.comboBuyStg.setCurrentIndex(0)
                    # 첫 번째 매수전략 내용 로드
                    self.load_strategy_content(buy_strategies[0], 'buy')
                    
        except Exception as ex:
            logging.error(f"매수전략 로드 실패: {ex}")
    
    def load_sell_strategies(self):
        """매도전략 콤보박스 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            self.comboSellStg.clear()
            current_strategy = self.comboStg.currentText()
            
            if config.has_section(current_strategy):
                sell_strategies = []
                for key, value in config.items(current_strategy):
                    if key.startswith('sell_stg_'):
                        try:
                            strategy_data = eval(value)  # JSON 파싱
                            if isinstance(strategy_data, dict) and 'name' in strategy_data:
                                sell_strategies.append(strategy_data['name'])
                        except:
                            continue
                
                for strategy_name in sell_strategies:
                    self.comboSellStg.addItem(strategy_name)
                
                if sell_strategies:
                    self.comboSellStg.setCurrentIndex(0)
                    # 첫 번째 매도전략 내용 로드
                    self.load_strategy_content(sell_strategies[0], 'sell')
                    
        except Exception as ex:
            logging.error(f"매도전략 로드 실패: {ex}")
    
    def load_initial_strategy_content(self):
        """초기 전략 내용을 텍스트박스에 로드"""
        try:
            # 매수전략 초기 내용 로드
            if self.comboBuyStg.count() > 0:
                current_buy_strategy = self.comboBuyStg.currentText()
                self.load_strategy_content(current_buy_strategy, 'buy')
            
            # 매도전략 초기 내용 로드
            if self.comboSellStg.count() > 0:
                current_sell_strategy = self.comboSellStg.currentText()
                self.load_strategy_content(current_sell_strategy, 'sell')
                
        except Exception as ex:
            logging.error(f"초기 전략 내용 로드 실패: {ex}")
    
    def add_stock_to_list(self):
        """투자 대상 종목 리스트에 종목 추가"""
        try:
            stock_input = self.stockInputEdit.text().strip()
            if not stock_input:
                logging.warning("종목명 또는 종목코드를 입력해주세요.")
                return
            
            # 종목코드 정규화 (6자리 숫자로 변환)
            stock_code, stock_name = self.normalize_stock_input(stock_input)
            
            # 중복 확인
            for i in range(self.monitoringBox.count()):
                item_text = self.monitoringBox.item(i).text()
                if stock_code in item_text or stock_name in item_text:
                    logging.warning(f"'{stock_name}' 종목이 이미 리스트에 있습니다.")
                    return
            
            # 리스트에 추가 (종목코드 - 종목명 형식)
            list_item_text = f"{stock_code} - {stock_name}"
            self.monitoringBox.addItem(list_item_text)
            logging.info(f"📋 모니터링 리스트에 종목 추가: {list_item_text}")
            
            # 입력 필드 초기화
            self.stockInputEdit.clear()
            
            # 웹소켓 기능이 제거됨 - 별도로 관리됨
            
            logging.info(f"투자 대상 종목 추가: {list_item_text}")
            
        except Exception as ex:
            logging.error(f"종목 추가 실패: {ex}")
    
    def normalize_stock_input(self, stock_input):
        """종목 입력값을 정규화하여 종목코드와 종목명 반환"""
        try:
            # 숫자만 있는 경우 (종목코드)
            if stock_input.isdigit():
                if len(stock_input) == 6:
                    # 종목코드로 종목명 조회 (간단한 예시)
                    stock_name = self.get_stock_name_by_code(stock_input)
                    return stock_input, stock_name
                else:
                    # 6자리가 아닌 경우 앞에 0을 붙여서 6자리로 만듦
                    stock_code = stock_input.zfill(6)
                    stock_name = self.get_stock_name_by_code(stock_code)
                    return stock_code, stock_name
            
            # 한글 종목명인 경우
            elif any('\uac00' <= char <= '\ud7af' for char in stock_input):
                # 종목명으로 종목코드 조회 (간단한 예시)
                stock_code = self.get_stock_code_by_name(stock_input)
                return stock_code, stock_input
            
            # 영문 종목명인 경우
            elif stock_input.isalpha():
                stock_code = self.get_stock_code_by_name(stock_input)
                return stock_code, stock_input
            
            else:
                # 기타 경우 그대로 사용
                return stock_input, stock_input
                    
        except Exception as ex:
            logging.error(f"종목 입력 정규화 실패: {ex}")
            return stock_input, stock_input
    
    def get_stock_name_by_code(self, stock_code):
        """종목코드로 종목명 조회"""
        try:
            # 주요 종목코드 매핑 (실제로는 API나 DB에서 조회)
            stock_mapping = {
                "005930": "삼성전자",
                "005380": "현대차",
                "000660": "SK하이닉스", 
                "035420": "NAVER",
                "207940": "삼성바이오로직스",
                "006400": "삼성SDI",
                "051910": "LG화학",
                "035720": "카카오",
                "068270": "셀트리온",
                "323410": "카카오뱅크",
                "000270": "기아"
            }
            
            return stock_mapping.get(stock_code, f"종목({stock_code})")
            
        except Exception as ex:
            logging.error(f"종목명 조회 실패: {ex}")
            return f"종목({stock_code})"
    
    def get_stock_code_by_name(self, stock_name):
        """종목명으로 종목코드 조회"""
        try:
            # 주요 종목명 매핑 (실제로는 API나 DB에서 조회)
            stock_mapping = {
                "삼성전자": "005930",
                "현대차": "005380",
                "SK하이닉스": "000660",
                "NAVER": "035420", 
                "네이버": "035420",
                "삼성바이오로직스": "207940",
                "삼성SDI": "006400",
                "LG화학": "051910",
                "카카오": "035720",
                "셀트리온": "068270",
                "카카오뱅크": "323410",
                "기아": "000270"
            }
            
            return stock_mapping.get(stock_name, stock_name)
            
        except Exception as ex:
            logging.error(f"종목코드 조회 실패: {ex}")
            return stock_name
    
    def get_monitoring_stock_codes(self):
        """모니터링 박스에서 종목 코드 리스트 추출"""
        try:
            codes = []
            for i in range(self.monitoringBox.count()):
                item_text = self.monitoringBox.item(i).text()
                # "종목코드 - 종목명" 형식에서 종목코드만 추출
                if " - " in item_text:
                    code = item_text.split(" - ")[0]
                    codes.append(code)
                elif " " in item_text:
                    # 공백으로 구분된 경우 첫 번째 부분이 종목코드인지 확인
                    parts = item_text.split(" ")
                    if len(parts[0]) == 6 and parts[0].isdigit():
                        codes.append(parts[0])
                else:
                    # 단일 종목코드인 경우
                    if len(item_text) == 6 and item_text.isdigit():
                        codes.append(item_text)
            
            return codes
            
        except Exception as ex:
            logging.error(f"모니터링 종목 코드 추출 실패: {ex}")
            return [
                '005930', '005380', '000660', '035420', '207940', '006400', 
                '051910', '035720', '068270', '323410', '000270'
            ]  # 기본값으로 주요 종목들 반환
    
    
    def buycount_setting(self):
        """투자 종목수 설정"""
        try:
            buycount = int(self.buycountEdit.text())
            if buycount > 0:
                logging.info(f"최대 투자 종목수 설정: {buycount}")
                if hasattr(self, 'trader'):
                    self.trader.buycount = buycount
            else:
                logging.warning("1 이상의 숫자를 입력해주세요.")
        except ValueError:
            logging.warning("올바른 숫자를 입력해주세요.")
        except Exception as ex:
            logging.error(f"투자 종목수 설정 실패: {ex}")
    
    def buy_item(self):
        """종목 매입 (키움 REST API 기반)"""
        try:
            current_item = self.monitoringBox.currentItem()
            if current_item:
                item_text = current_item.text()
                # "종목코드 - 종목명" 형식에서 종목코드만 추출
                code = item_text.split(' - ')[0] if ' - ' in item_text else item_text.split(' ')[0]
                name = item_text.split(' - ')[1] if ' - ' in item_text else "알 수 없음"
                
                logging.info(f"매입 요청: {code} - {name}")
                
                # 매수 수량 입력 받기
                quantity, ok = QInputDialog.getInt(self, "매수 수량", f"{name} 매수 수량을 입력하세요:", 1, 1, 1000)
                if not ok:
                    return
                
                # 매수 가격 입력 받기 (0이면 시장가)
                price, ok = QInputDialog.getInt(self, "매수 가격", f"{name} 매수 가격을 입력하세요 (0: 시장가):", 0, 0, 1000000)
                if not ok:
                    return
                
                # 키움 REST API를 통한 매수 주문
                if hasattr(self, 'kiwoom_client') and self.kiwoom_client:
                    success = self.kiwoom_client.place_buy_order(code, quantity, price, "market" if price == 0 else "limit")
                    
                    if success:
                        # 매수 성공 (실시간 잔고 데이터가 자동으로 보유 종목에 추가됨)
                        logging.info(f"✅ 매수 주문 성공: {code} - {name} {quantity}주")
                        QMessageBox.information(self, "매수 완료", f"{name} {quantity}주 매수 주문이 완료되었습니다.")
                    else:
                        logging.error(f"❌ 매수 주문 실패: {code} - {name}")
                        QMessageBox.warning(self, "매수 실패", f"{name} 매수 주문이 실패했습니다.")
                else:
                    logging.error("키움 클라이언트가 초기화되지 않았습니다")
                    QMessageBox.warning(self, "오류", "키움 클라이언트가 초기화되지 않았습니다.")
            else:
                logging.warning("매입할 종목을 선택해주세요.")
                QMessageBox.warning(self, "선택 오류", "매입할 종목을 선택해주세요.")
        except Exception as ex:
            logging.error(f"매입 실패: {ex}")
            QMessageBox.critical(self, "매입 오류", f"매입 중 오류가 발생했습니다: {ex}")
    
    def delete_select_item(self):
        """선택된 종목 삭제"""
        try:
            current_item = self.monitoringBox.currentItem()
            if current_item:
                self.monitoringBox.takeItem(self.monitoringBox.row(current_item))
                
                # 웹소켓 기능이 제거됨 - 별도로 관리됨
                
                logging.info("선택된 종목이 삭제되었습니다.")
            else:
                logging.warning("삭제할 종목을 선택해주세요.")
        except Exception as ex:
            logging.error(f"종목 삭제 실패: {ex}")
    
    def sell_item(self):
        """종목 매도 (키움 REST API 기반)"""
        try:
            current_item = self.boughtBox.currentItem()
            if current_item:
                item_text = current_item.text()
                # "종목코드 - 종목명" 형식에서 종목코드만 추출
                code = item_text.split(' - ')[0] if ' - ' in item_text else item_text.split(' ')[0]
                name = item_text.split(' - ')[1] if ' - ' in item_text else "알 수 없음"
                
                logging.info(f"매도 요청: {code} - {name}")
                
                # 매도 수량 입력 받기
                quantity, ok = QInputDialog.getInt(self, "매도 수량", f"{name} 매도 수량을 입력하세요:", 1, 1, 1000)
                if not ok:
                    return
                
                # 매도 가격 입력 받기 (0이면 시장가)
                price, ok = QInputDialog.getInt(self, "매도 가격", f"{name} 매도 가격을 입력하세요 (0: 시장가):", 0, 0, 1000000)
                if not ok:
                    return
                
                # 키움 REST API를 통한 매도 주문
                if hasattr(self, 'kiwoom_client') and self.kiwoom_client:
                    success = self.kiwoom_client.place_sell_order(code, quantity, price, "market" if price == 0 else "limit")
                    
                    if success:
                        # 매도 성공 (실시간 잔고 데이터가 자동으로 보유 종목에서 제거됨)
                        logging.info(f"✅ 매도 주문 성공: {code} - {name} {quantity}주")
                        QMessageBox.information(self, "매도 완료", f"{name} {quantity}주 매도 주문이 완료되었습니다.")
                    else:
                        logging.error(f"❌ 매도 주문 실패: {code} - {name}")
                        QMessageBox.warning(self, "매도 실패", f"{name} 매도 주문이 실패했습니다.")
                else:
                    logging.error("키움 클라이언트가 초기화되지 않았습니다")
                    QMessageBox.warning(self, "오류", "키움 클라이언트가 초기화되지 않았습니다.")
            else:
                logging.warning("매도할 종목을 선택해주세요.")
                QMessageBox.warning(self, "선택 오류", "매도할 종목을 선택해주세요.")
        except Exception as ex:
            logging.error(f"매도 실패: {ex}")
            QMessageBox.critical(self, "매도 오류", f"매도 중 오류가 발생했습니다: {ex}")
    
    def sell_all_item(self):
        """전체 매도 (키움 REST API 기반)"""
        try:
            if self.boughtBox.count() > 0:
                # 확인 대화상자
                reply = QMessageBox.question(self, "전체 매도 확인", 
                                           "보유 중인 모든 종목을 매도하시겠습니까?",
                                           QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                
                if reply == QMessageBox.StandardButton.Yes:
                    logging.info("전체 매도 요청")
                    
                    # 보유 종목 목록 생성
                    sell_items = []
                    for i in range(self.boughtBox.count()):
                        item = self.boughtBox.item(i)
                        item_text = item.text()
                        code = item_text.split(' - ')[0] if ' - ' in item_text else item_text.split(' ')[0]
                        name = item_text.split(' - ')[1] if ' - ' in item_text else "알 수 없음"
                        sell_items.append((code, name))
                    
                    # 각 종목에 대해 매도 주문 실행
                    success_count = 0
                    for code, name in sell_items:
                        try:
                            if hasattr(self, 'kiwoom_client') and self.kiwoom_client:
                                # 시장가로 매도 (수량은 1로 설정, 실제로는 보유 수량을 조회해야 함)
                                success = self.kiwoom_client.place_sell_order(code, 1, 0, "market")
                                
                                if success:
                                    success_count += 1
                                    # 매도 성공 (실시간 잔고 데이터가 자동으로 보유 종목에서 제거됨)
                                    logging.info(f"✅ 전체 매도 성공: {code} - {name}")
                                else:
                                    logging.error(f"❌ 전체 매도 실패: {code} - {name}")
                        except Exception as item_ex:
                            logging.error(f"❌ {code} 매도 중 오류: {item_ex}")
                    
                    # 결과 메시지
                    if success_count > 0:
                        QMessageBox.information(self, "전체 매도 완료", 
                                              f"{success_count}개 종목의 매도 주문이 완료되었습니다.")
                    else:
                        QMessageBox.warning(self, "전체 매도 실패", 
                                          "매도 주문이 실패했습니다.")
                else:
                    logging.info("전체 매도 취소됨")
            else:
                logging.warning("매도할 종목이 없습니다.")
                QMessageBox.information(self, "알림", "매도할 종목이 없습니다.")
        except Exception as ex:
            logging.error(f"전체 매도 실패: {ex}")
            QMessageBox.critical(self, "전체 매도 오류", f"전체 매도 중 오류가 발생했습니다: {ex}")
    
    

    def listBoxChanged(self, current):
        """리스트박스 클릭 이벤트 - 차트 표시"""
        logging.info(f"🔍 listBoxChanged 호출됨 - current: {current}")
        
        
        # 중복 호출 방지를 위한 락
        if not self.chart_drawing_lock.acquire(blocking=False):
            logging.warning("📊 listBoxChanged is already running. Skipping duplicate call.")
            return
        
        # ChartDrawer가 처리 중인지 확인
        if (hasattr(self, 'chartdrawer') and self.chartdrawer and 
            hasattr(self.chartdrawer, '_is_processing') and self.chartdrawer._is_processing):
            logging.warning(f"📊 ChartDrawer가 이미 차트를 생성 중입니다 ({self.chartdrawer._processing_code}). 중복 실행 방지.")
            self.chart_drawing_lock.release()
            return
        
        try:
            if current:
                item_text = current.text()
                logging.info(f"🔍 선택된 아이템 텍스트: {item_text}")
                
                # 리스트박스 상태 확인
                logging.info(f"🔍 monitoringBox 아이템 수: {self.monitoringBox.count()}")
                logging.info(f"🔍 boughtBox 아이템 수: {self.boughtBox.count()}")
                logging.info(f"🔍 monitoringBox 현재 선택: {self.monitoringBox.currentItem()}")
                logging.info(f"🔍 boughtBox 현재 선택: {self.boughtBox.currentItem()}")
                
                # "종목코드 - 종목명" 형식에서 종목코드와 종목명 추출
                parts = item_text.split(' - ')
                code = parts[0]
                name = parts[1] if len(parts) > 1 else self.get_stock_name_by_code(code) # Fallback
                
                logging.info(f"📊 종목 클릭됨: {item_text} -> 종목코드: {code}, 종목명: {name}")
                
                # chartdrawer 객체 존재 확인 및 초기화 시도
                logging.info(f"🔍 chartdrawer 상태 확인 - hasattr: {hasattr(self, 'chartdrawer')}, is None: {not hasattr(self, 'chartdrawer') or self.chartdrawer is None}")
                if not hasattr(self, 'chartdrawer') or not self.chartdrawer:
                    logging.warning("⚠️ chartdrawer 객체가 아직 초기화되지 않았습니다.")
                    # ChartDrawer 초기화 로직
                    if not self._ensure_chart_drawer_initialized():
                        # 초기화에 실패하면 여기서 중단
                        logging.error("❌ ChartDrawer 최종 초기화 실패. 차트 표시를 중단합니다.")
                        return

                # 이제 chartdrawer가 확실히 존재하므로 차트 표시
                if self.chartdrawer:
                    logging.info(f"차트 표시 시작: {code}")
                    self.chartdrawer.set_code(code, name)
            else:
                logging.info("🔍 current가 None입니다 - 종목 선택 해제됨")
                if hasattr(self, 'chartdrawer') and self.chartdrawer:
                    self.chartdrawer.set_code(None)
        except Exception as ex:
            logging.error(f"리스트박스 변경 이벤트 처리 실패: {ex}")
        finally:
            # 처리 완료 후 락 해제
            self.chart_drawing_lock.release()
    
    def print_chart(self):
        """차트 출력"""
        try:
            logging.info("차트 출력 기능은 준비 중입니다.")
        except Exception as ex:
            logging.error(f"차트 출력 실패: {ex}")
    
    def output_current_data(self):
        """현재 데이터 출력"""
        try:
            logging.info("데이터 저장 기능은 준비 중입니다.")
        except Exception as ex:
            logging.error(f"데이터 저장 실패: {ex}")
    
    def stgChanged(self):
        """전략 변경"""
        try:
            strategy_name = self.comboStg.currentText()
            logging.info(f"투자 전략 변경: {strategy_name}")
            
            # 현재 선택된 전략을 settings.ini에 저장
            self.save_current_strategy()
            
            # 조건검색식인지 확인 (조건검색 목록에 있는지 확인)
            if hasattr(self, 'condition_search_list') and self.condition_search_list:
                condition_names = [condition['title'] for condition in self.condition_search_list]
                if strategy_name in condition_names:
                    # 조건검색식 선택 시 바로 실행 (비동기)
                    import asyncio
                    asyncio.create_task(self.handle_condition_search())
                    return
            
            # 통합 전략인 경우 모든 조건검색식 실행
            if strategy_name == "통합 전략":
                if hasattr(self, 'condition_search_list') and self.condition_search_list:
                    logging.info("🔍 통합 전략 실행: 모든 조건검색식 적용")
                    import asyncio
                    asyncio.create_task(self.handle_integrated_condition_search())
                    return
            
            # 일반 투자전략인 경우 기존 로직 실행
            # 투자전략 변경 시 매수/매도 전략도 업데이트
            self.load_buy_strategies()
            self.load_sell_strategies()
            
            # 변경된 전략의 첫 번째 매수/매도 전략 내용 자동 로드
            self.load_initial_strategy_content()
            
        except Exception as ex:
            logging.error(f"전략 변경 실패: {ex}")
    
    def buyStgChanged(self):
        """매수 전략 변경"""
        try:
            strategy_name = self.comboBuyStg.currentText()
            logging.info(f"매수 전략 변경: {strategy_name}")
            
            # 매수 전략 내용을 텍스트 위젯에 표시
            self.load_strategy_content(strategy_name, 'buy')
            
        except Exception as ex:
            logging.error(f"매수 전략 변경 실패: {ex}")
    
    def sellStgChanged(self):
        """매도 전략 변경"""
        try:
            strategy_name = self.comboSellStg.currentText()
            logging.info(f"매도 전략 변경: {strategy_name}")
            
            # 매도 전략 내용을 텍스트 위젯에 표시
            self.load_strategy_content(strategy_name, 'sell')
            
        except Exception as ex:
            logging.error(f"매도 전략 변경 실패: {ex}")
    
    def load_strategy_content(self, strategy_name, strategy_type):
        """전략 내용을 텍스트 위젯에 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            current_strategy = self.comboStg.currentText()
            if not config.has_section(current_strategy):
                return
            
            # 전략 키 찾기
            strategy_key = None
            for key, value in config.items(current_strategy):
                try:
                    strategy_data = eval(value)
                    if isinstance(strategy_data, dict) and strategy_data.get('name') == strategy_name:
                        if strategy_type == 'buy' and key.startswith('buy_stg_'):
                            strategy_key = key
                            break
                        elif strategy_type == 'sell' and key.startswith('sell_stg_'):
                            strategy_key = key
                            break
                except:
                    continue
            
            if strategy_key:
                strategy_data = eval(config.get(current_strategy, strategy_key))
                content = strategy_data.get('content', '')
                
                if strategy_type == 'buy':
                    self.buystgInputWidget.setPlainText(content)
                elif strategy_type == 'sell':
                    self.sellstgInputWidget.setPlainText(content)
                    
        except Exception as ex:
            logging.error(f"전략 내용 로드 실패: {ex}")
    
    def load_backtest_strategies(self):
        """백테스팅 전략 콤보박스 로드"""
        try:
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            self.bt_strategy_combo.clear()
            if config.has_section('STRATEGIES'):
                for key, value in config.items('STRATEGIES'):
                    if key.startswith('stg_') or key == 'stg_integrated':
                        self.bt_strategy_combo.addItem(value)
            
            # 기본 전략 설정
            if config.has_option('SETTINGS', 'last_strategy'):
                last_strategy = config.get('SETTINGS', 'last_strategy')
                index = self.bt_strategy_combo.findText(last_strategy)
                if index >= 0:
                    self.bt_strategy_combo.setCurrentIndex(index)
            
            logging.info("백테스팅 전략 콤보박스 로드 완료")
            
        except Exception as ex:
            logging.error(f"백테스팅 전략 콤보박스 로드 실패: {ex}")
    
    def save_buystrategy(self):
        """매수 전략 저장"""
        try:
            strategy_text = self.buystgInputWidget.toPlainText()
            current_strategy = self.comboStg.currentText()
            current_buy_strategy = self.comboBuyStg.currentText()
            
            # settings.ini 파일 업데이트
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # 해당 전략의 매수 전략 내용 업데이트
            for key, value in config.items(current_strategy):
                try:
                    strategy_data = eval(value)
                    if isinstance(strategy_data, dict) and strategy_data.get('name') == current_buy_strategy:
                        if key.startswith('buy_stg_'):
                            strategy_data['content'] = strategy_text
                            config.set(current_strategy, key, str(strategy_data))
                            break
                except:
                    continue
            
            # 파일 저장
            with open('settings.ini', 'w', encoding='utf-8') as configfile:
                config.write(configfile)
            
            logging.info(f"매수 전략 '{current_buy_strategy}'이 저장되었습니다.")
        except Exception as ex:
            logging.error(f"매수 전략 저장 실패: {ex}")
    
    def save_sellstrategy(self):
        """매도 전략 저장"""
        try:
            strategy_text = self.sellstgInputWidget.toPlainText()
            current_strategy = self.comboStg.currentText()
            current_sell_strategy = self.comboSellStg.currentText()
            
            # settings.ini 파일 업데이트
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # 해당 전략의 매도 전략 내용 업데이트
            for key, value in config.items(current_strategy):
                try:
                    strategy_data = eval(value)
                    if isinstance(strategy_data, dict) and strategy_data.get('name') == current_sell_strategy:
                        if key.startswith('sell_stg_'):
                            strategy_data['content'] = strategy_text
                            config.set(current_strategy, key, str(strategy_data))
                            break
                except:
                    continue
            
            # 파일 저장
            with open('settings.ini', 'w', encoding='utf-8') as configfile:
                config.write(configfile)
            
            logging.info(f"매도 전략 '{current_sell_strategy}'이 저장되었습니다.")
        except Exception as ex:
            logging.error(f"매도 전략 저장 실패: {ex}")
    
    def load_db_period(self):
        """DB 기간 불러오기"""
        try:
            # DB에서 날짜 범위 조회
            import sqlite3
            conn = sqlite3.connect('vi_stock_data.db')
            cursor = conn.cursor()
            
            cursor.execute("SELECT MIN(datetime), MAX(datetime) FROM stock_data")
            result = cursor.fetchone()
            conn.close()
            
            if result and result[0] and result[1]:
                start_date = result[0][:10].replace('-', '')
                end_date = result[1][:10].replace('-', '')
                self.bt_start_date.setText(start_date)
                self.bt_end_date.setText(end_date)
                logging.info(f"DB 기간 로드: {start_date} ~ {end_date}")
            else:
                logging.warning("DB에서 날짜 정보를 찾을 수 없습니다.")
                
        except Exception as ex:
            logging.error(f"DB 기간 로드 실패: {ex}")
    
    def run_backtest(self):
        """백테스팅 실행"""
        try:
            logging.info("백테스팅 기능은 준비 중입니다.")
        except Exception as ex:
            logging.error(f"백테스팅 실행 실패: {ex}")

    def _ensure_chart_drawer_initialized(self):
        """ChartDrawer가 초기화되었는지 확인하고, 그렇지 않으면 초기화를 시도합니다."""
        if hasattr(self, 'chartdrawer') and self.chartdrawer:
            return True

        logging.info("💡 ChartDrawer 초기화를 시작합니다.")
        
        # 재시도 횟수 확인
        if self.chart_init_retry_count >= self.max_chart_init_retries:
            logging.error(f"❌ ChartDrawer 초기화 최대 재시도 횟수({self.max_chart_init_retries}) 초과")
            # 에러 플레이스홀더 표시
            self._show_chart_error_placeholder()
            return False

        # 차트 브라우저가 준비되었는지 확인하고, 그렇지 않으면 초기화 시도
        if not (hasattr(self, 'chart_browser') and self.chart_browser):
            logging.warning("⚠️ 차트 브라우저가 아직 준비되지 않았습니다. 강제 초기화를 시도합니다.")
            self.chart_init_retry_count += 1
            if not self._safe_initialize_chart_browser(self.chart_layout):
                logging.error("❌ 차트 브라우저 강제 초기화 실패.")
                self._show_chart_error_placeholder()
                return False
        
        # ChartDrawer 초기화 시도
        if not self._initialize_chart_drawer():
            self.chart_init_retry_count += 1
            logging.warning(f"⚠️ ChartDrawer 초기화 실패. 재시도 횟수: {self.chart_init_retry_count}/{self.max_chart_init_retries}")
            self._show_chart_error_placeholder()
            return False

        logging.info("✅ ChartDrawer 초기화 성공!")
        self.chart_init_retry_count = 0  # 성공 시 재시도 카운터 리셋
        return True

    def _show_chart_error_placeholder(self):
        """차트 에러 플레이스홀더를 표시합니다."""
        try:
            if hasattr(self, 'chart_layout') and self.chart_layout:
                # 기존 위젯 제거
                while self.chart_layout.count():
                    item = self.chart_layout.takeAt(0)
                    widget = item.widget()
                    if widget:
                        widget.deleteLater()
                
                # 에러 플레이스홀더 추가
                error_placeholder = self._create_error_placeholder_widget()
                if error_placeholder:
                    self.chart_layout.addWidget(error_placeholder)
        except Exception as ex:
            logging.error(f"❌ 에러 플레이스홀더 표시 실패: {ex}")

    
    def _safe_initialize_chart_browser(self, chartLayout):
        """안전한 차트 브라우저 초기화 (예외 처리 강화)"""
        try:
            logging.info("🔧 안전한 차트 브라우저 초기화 시작...")
            
            # 이미 초기화되었는지 확인
            if hasattr(self, 'chart_browser') and self.chart_browser:
                logging.info("✅ 차트 브라우저가 이미 초기화되었습니다")
                return True
            
            # WebEngine 사용 가능 여부 재확인
            if not self.webengine_available:
                logging.error("❌ WebEngine 사용 불가 - 차트 기능을 사용할 수 없습니다")
                return False
            
            # 차트 브라우저 초기화 시도
            return self._initialize_chart_browser(chartLayout)
            
        except Exception as ex:
            logging.error(f"❌ 안전한 차트 브라우저 초기화 실패: {ex}")
            import traceback
            logging.error(f"차트 브라우저 초기화 에러 상세: {traceback.format_exc()}")
            
            # 에러 발생 시 플레이스홀더 유지
            try:
                if chartLayout.count() == 0:
                    chartLayout.addWidget(self._create_error_placeholder_widget())
            except Exception as placeholder_ex:
                logging.error(f"❌ 에러 플레이스홀더 생성 실패: {placeholder_ex}")
            
            return False
    
    def _create_error_placeholder_widget(self):
        """차트 초기화 실패 시 에러 플레이스홀더 생성"""
        try:
            from PyQt6.QtWidgets import QLabel
            placeholder = QLabel("❌ 차트 초기화 실패\nWebEngine을 설치해주세요")
            placeholder.setStyleSheet("""
                QLabel {
                    background-color: #ffe6e6;
                    border: 2px solid #ff0000;
                    font-size: 14px;
                    color: #cc0000;
                    padding: 50px;
                    text-align: center;
                }
            """)
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            return placeholder
        except Exception as ex:
            logging.error(f"❌ 에러 플레이스홀더 생성 실패: {ex}")
            return None
    
    def _add_chart_browser_to_layout(self, chartLayout):
        """차트 브라우저를 레이아웃에 안전하게 추가"""
        try:
            if hasattr(self, 'chart_browser') and self.chart_browser:
                chartLayout.addWidget(self.chart_browser)
                logging.info("✅ 차트 브라우저를 레이아웃에 추가 완료")
            else:
                logging.warning("⚠️ 차트 브라우저가 유효하지 않음")
        except Exception as ex:
            logging.error(f"❌ 차트 브라우저 레이아웃 추가 실패: {ex}")

    def _initialize_chart_browser(self, chartLayout):
        """차트 브라우저 초기화 (지연된 초기화)"""
        try:
            logging.info("🔧 차트 브라우저 지연 초기화 시작...")
            
            # 이미 초기화되었는지 확인
            if hasattr(self, 'chart_browser') and self.chart_browser:
                logging.info("✅ 차트 브라우저가 이미 초기화되었습니다")
                return True
            

            
            # QWebEngineView 사용 (Plotly JavaScript 지원) - PyQt6 호환
            if self.webengine_available:
                try:
                    # PyQt6용 QWebEngineView 임포트
                    from PyQt6.QtWebEngineWidgets import QWebEngineView
                    from PyQt6.QtWebEngineCore import QWebEngineSettings
                    logging.info("✅ PyQt6 QWebEngineView 로드 성공")
                    
                    # WebEngine 프로필 초기화 (필요한 경우)
                    try:
                        from PyQt6.QtWebEngineCore import QWebEngineProfile
                        profile = QWebEngineProfile.defaultProfile()
                        profile.setHttpUserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                        logging.info("✅ WebEngine 프로필 초기화 완료")
                    except Exception as e:
                        logging.warning(f"⚠️ WebEngine 프로필 초기화 실패 (무시): {e}")
                    
                    self.chart_browser = QWebEngineView()
                    
                    # QWebEngineView가 별도 창으로 표시되지 않도록 설정
                    try:
                        # 창 속성을 더 안전하게 설정
                        self.chart_browser.setWindowFlags(Qt.WindowType.Widget)
                        self.chart_browser.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
                        self.chart_browser.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, False)
                        self.chart_browser.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
                        self.chart_browser.setAttribute(Qt.WidgetAttribute.WA_StaticContents, True)
                        logging.info("✅ QWebEngineView 창 속성 설정 완료")
                    except Exception as attr_ex:
                        logging.warning(f"⚠️ QWebEngineView 창 속성 설정 실패 (무시): {attr_ex}")
                    
                    # 차트 브라우저 크기 설정
                    try:
                        self.chart_browser.setMinimumSize(800, 600)
                        self.chart_browser.resize(800, 600)
                        logging.info("✅ QWebEngineView 크기 설정 완료")
                    except Exception as size_ex:
                        logging.warning(f"⚠️ QWebEngineView 크기 설정 실패 (무시): {size_ex}")
                    
                    # 초기에는 숨김 상태로 설정 (레이아웃에 추가 후 표시)
                    self.chart_browser.setVisible(False)
                    
                    # WebEngine 오류 처리 설정 (PyQt6 호환)
                    try:
                        from PyQt6.QtWebEngineCore import QWebEnginePage
                        page = self.chart_browser.page()
                        if page:
                            # JavaScript 콘솔 메시지 처리
                            def handle_console_message(level, message, line_number, source_id):
                                if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessage:
                                    logging.error(f"❌ JavaScript 오류: {message} (라인 {line_number})")
                                elif level == QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessage:
                                    logging.warning(f"⚠️ JavaScript 경고: {message} (라인 {line_number})")
                                else:
                                    logging.info(f"📊 JavaScript 로그: {message}")
                            
                            # PyQt6에서는 시그널 연결 방식이 다름 - 시그널 객체를 직접 사용
                            try:
                                # PyQt6에서는 시그널이 메서드가 아니라 속성으로 접근
                                if hasattr(page, 'javaScriptConsoleMessage') and hasattr(page.javaScriptConsoleMessage, 'connect'):
                                    page.javaScriptConsoleMessage.connect(handle_console_message)
                                    logging.info("✅ WebEngine 오류 처리 설정 완료")
                                else:
                                    # 대안: 시그널 연결을 시도하지 않고 로깅만 활성화
                                    logging.info("✅ WebEngine JavaScript 로깅 활성화 (시그널 연결 생략)")
                            except AttributeError:
                                logging.info("✅ WebEngine JavaScript 로깅 활성화 (시그널 연결 생략)")
                    except Exception as e:
                        logging.warning(f"⚠️ WebEngine 오류 처리 설정 실패: {e}")
                    
                    # WebEngine 설정 최적화
                    settings = self.chart_browser.settings()
                    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
                    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
                    settings.setAttribute(QWebEngineSettings.WebAttribute.ErrorPageEnabled, True)
                    settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, True)
                    settings.setAttribute(QWebEngineSettings.WebAttribute.AutoLoadImages, True)
                    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
                    settings.setAttribute(QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, True)
                    
                    # WebEngine 프로필 설정
                    try:
                        from PyQt6.QtWebEngineCore import QWebEngineProfile
                        profile = QWebEngineProfile.defaultProfile()
                        profile.setHttpUserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
                        logging.info("✅ WebEngine 프로필 설정 완료")
                    except Exception as e:
                        logging.warning(f"⚠️ WebEngine 프로필 설정 실패: {e}")
                    
                    self.chart_browser.setStyleSheet("""
                        QWebEngineView {
                            background-color: white;
                            border: 2px solid #007acc;
                            border-radius: 5px;
                            margin: 5px;
                        }
                        QWebEngineView:hover {
                            border-color: #005a9e;
                        }
                    """)
                    
                    # WebEngine 테스트 - 간단한 차트 먼저 표시
                    test_html = """
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <title>WebEngine 차트 테스트</title>
                        <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
                        <style>
                            body { 
                                font-family: Arial, sans-serif; 
                                padding: 20px; 
                                background-color: #f0f8ff; 
                                margin: 0;
                            }
                            .test-box { 
                                background-color: white; 
                                border: 3px solid #007acc; 
                                padding: 30px; 
                                margin: 10px 0;
                                border-radius: 10px;
                                box-shadow: 0 4px 8px rgba(0,0,0,0.2);
                                text-align: center;
                            }
                            #testChart { width: 100%; height: 500px; }
                            h3 { color: #007acc; margin-bottom: 20px; }
                            button { 
                                background-color: #007acc; 
                                color: white; 
                                padding: 10px 20px; 
                                border: none; 
                                border-radius: 5px; 
                                cursor: pointer;
                                font-size: 16px;
                            }
                            button:hover { background-color: #005a9e; }
                        </style>
                    </head>
                    <body>
                        <div class="test-box">
                            <h3>🚀 WebEngine 차트 테스트</h3>
                            <p>차트 영역이 정상적으로 표시되면 WebEngine이 올바르게 작동합니다.</p>
                            <div id="testChart"></div>
                            <button onclick="createTestChart()">📊 테스트 차트 생성</button>
                        </div>
                        <script>
                            function createTestChart() {
                                var data = [{
                                    x: ['1월', '2월', '3월', '4월', '5월'],
                                    y: [10, 15, 13, 17, 20],
                                    type: 'scatter',
                                    mode: 'lines+markers',
                                    name: '테스트 데이터'
                                }];
                                
                                var layout = {
                                    title: '테스트 차트',
                                    xaxis: { title: '월' },
                                    yaxis: { title: '값' }
                                };
                                
                                Plotly.newPlot('testChart', data, layout);
                                console.log('테스트 차트 생성 완료');
                            }
                            
                            // 페이지 로드 시 자동으로 차트 생성
                            window.onload = function() {
                                createTestChart();
                            };
                        </script>
                    </body>
                    </html>
                    """
                    self.chart_browser.setHtml(test_html)
                    logging.info("✅ QWebEngineView 초기화 성공 - Plotly 차트 지원")
                    logging.info("📊 차트 브라우저 크기: {}x{}".format(self.chart_browser.width(), self.chart_browser.height()))
                    logging.info("📊 차트 브라우저 가시성: {}".format(self.chart_browser.isVisible()))
                except Exception as e:
                    logging.error(f"❌ QWebEngineView 초기화 실패: {e}")
                    import traceback
                    logging.error(f"QWebEngineView 초기화 에러 상세: {traceback.format_exc()}")
                    self.webengine_available = False
                    
                    # 초기화 실패 시 에러 플레이스홀더 표시
                    try:
                        error_placeholder = self._create_error_placeholder_widget()
                        if error_placeholder:
                            chartLayout.addWidget(error_placeholder)
                            logging.info("✅ 에러 플레이스홀더 표시 완료")
                    except Exception as placeholder_ex:
                        logging.error(f"❌ 에러 플레이스홀더 표시 실패: {placeholder_ex}")
            else:
                logging.error("❌ WebEngine 사용 불가 - 차트 기능을 사용할 수 없습니다")
                logging.error("❌ 인터랙티브 차트를 사용하려면 'pip install PyQt6-WebEngine' 실행")
                return False
            
            # 차트 브라우저를 레이아웃에 추가 (더 안전한 방법)
            try:
                # 부모 윈도우 설정을 먼저 수행
                self.chart_browser.setParent(self)
                
                # 레이아웃에 추가하기 전에 잠시 대기
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(100, lambda: self._add_chart_browser_to_layout(chartLayout))
                logging.info("✅ 차트 브라우저 레이아웃 추가 예약 완료")
            except Exception as layout_ex:
                logging.error(f"❌ 차트 브라우저 레이아웃 추가 실패: {layout_ex}")
                return False
            
            # 차트 브라우저 표시를 위한 안전한 타이머 설정
            from PyQt6.QtCore import QTimer
            def show_chart_browser():
                try:
                    logging.info("📊 차트 브라우저 표시 시도 중...")
                    
                    # 차트 브라우저가 여전히 유효한지 확인
                    if not hasattr(self, 'chart_browser') or not self.chart_browser:
                        logging.warning("⚠️ 차트 브라우저가 더 이상 유효하지 않습니다")
                        return
                    
                    # 단계별로 안전하게 표시
                    self.chart_browser.setVisible(True)
                    self.chart_browser.show()
                    
                    # 부모 윈도우가 유효한지 확인 후 raise
                    if self.chart_browser.parent():
                        self.chart_browser.raise_()
                        self.chart_browser.activateWindow()
                    
                    # 표시 후 상태 확인
                    logging.info("📊 차트 브라우저 표시 완료")
                    logging.info("📊 차트 브라우저 크기: {}x{}".format(self.chart_browser.width(), self.chart_browser.height()))
                    logging.info("📊 차트 브라우저 가시성: {}".format(self.chart_browser.isVisible()))
                    logging.info("📊 차트 브라우저 부모: {}".format(self.chart_browser.parent()))
                    
                    # 차트 브라우저가 정상적으로 표시되었는지 확인
                    if self.chart_browser.isVisible() and self.chart_browser.width() > 0 and self.chart_browser.height() > 0:
                        logging.info("✅ 차트 브라우저 정상 표시 확인")
                    else:
                        logging.warning("⚠️ 차트 브라우저 표시 상태 이상")
                except Exception as e:
                    logging.error(f"❌ 차트 브라우저 표시 실패: {e}")
                    import traceback
                    logging.error(f"차트 브라우저 표시 에러 상세: {traceback.format_exc()}")
            
            # 500ms 후에 차트 브라우저 표시 (더 안전한 지연)
            QTimer.singleShot(500, show_chart_browser)
            
            # ChartDrawer 초기화 (차트 브라우저가 준비된 후)
            chart_drawer_success = self._initialize_chart_drawer()
            if chart_drawer_success:
                logging.info("✅ 차트 브라우저 및 ChartDrawer 초기화 완료")
            else:
                logging.warning("⚠️ 차트 브라우저는 초기화되었지만 ChartDrawer 초기화 실패")
            
            logging.info("✅ 차트 브라우저 지연 초기화 완료")
            return True
            
        except Exception as ex:
            logging.error(f"❌ 차트 브라우저 지연 초기화 실패: {ex}")
            import traceback
            logging.error(f"차트 브라우저 초기화 에러 상세: {traceback.format_exc()}")
            
            # 에러 발생 시 실패 반환
            logging.error("❌ 차트 브라우저 초기화 실패 - 차트 기능을 사용할 수 없습니다")
            return False
    
    def _initialize_chart_drawer(self):
        """ChartDrawer 초기화 (지연된 초기화)"""
        try:
            logging.info("🔧 ChartDrawer 지연 초기화 시작...")
            
            # 차트 브라우저가 준비되었는지 확인
            if not hasattr(self, 'chart_browser') or not self.chart_browser:
                logging.warning("⚠️ 차트 브라우저가 아직 준비되지 않았습니다")
                return False
            
            # ChartDrawer가 이미 초기화되었는지 확인
            if hasattr(self, 'chartdrawer') and self.chartdrawer:
                logging.info("✅ ChartDrawer가 이미 초기화되었습니다")
                return True
            
            # ChartDrawer 초기화
            self.chartdrawer = ChartDrawer(self.chart_browser, self)
            
            # ChartDrawer 시그널은 내부에서 자동 연결됨
            
            # 처리 상태 초기화
            if hasattr(self, '_processing_code'):
                self._processing_code = None
                logging.info("📊 ChartDrawer 초기화 시 처리 상태 초기화 완료")
            
            logging.info("✅ ChartDrawer 지연 초기화 완료")
            logging.info("📊 이제 종목을 클릭하여 차트를 볼 수 있습니다")
            return True
            
        except Exception as ex:
            logging.error(f"❌ ChartDrawer 지연 초기화 실패: {ex}")
            import traceback
            logging.error(f"ChartDrawer 초기화 에러 상세: {traceback.format_exc()}")
            return False

    async def post_login_setup(self):
        """로그인 후 설정"""
        try:
            # 로거 설정
            logger = logging.getLogger()
            if not any(isinstance(handler, QTextEditLogger) for handler in logger.handlers):
                text_edit_logger = QTextEditLogger(self.terminalOutput)
                text_edit_logger.setLevel(logging.INFO)
                logger.addHandler(text_edit_logger)

            # 1. 트레이더 객체 생성 (의존성 주입)
            if not hasattr(self, 'trader') or not self.trader:
                buycount = int(self.buycountEdit.text())
                self.trader = KiwoomTrader(self.login_handler.kiwoom_client, buycount, self)
                logging.info("✅ 트레이더 객체 생성 완료")

            # 2. 전략 객체 초기화
            if not self.objstg:
                self.objstg = KiwoomStrategy(self.trader, self)
                logging.debug("🔍 KiwoomStrategy 객체 생성 완료")

            # 3. 조건검색 목록조회 (웹소켓)
            try:
                # 웹소켓 클라이언트가 연결되어 있는지 확인
                if hasattr(self.login_handler, 'websocket_client') and self.login_handler.websocket_client:
                    if self.login_handler.websocket_client.connected:
                        # 웹소켓을 통한 조건검색 목록조회
                        await self.handle_condition_search_list_query()
                        logging.info("✅ 조건검색 목록조회 완료 (웹소켓)")
                    else:
                        logging.warning("⚠️ 웹소켓이 연결되지 않아 조건검색 목록조회를 건너뜁니다")
                        logging.info(f"🔍 웹소켓 연결 상태: connected={self.login_handler.websocket_client.connected}")
                else:
                    logging.warning("⚠️ 웹소켓 클라이언트가 없어 조건검색 목록조회를 건너뜁니다")
                    logging.info(f"🔍 login_handler.websocket_client 존재: {hasattr(self.login_handler, 'websocket_client')}")
                    if hasattr(self.login_handler, 'websocket_client'):
                        logging.info(f"🔍 websocket_client 값: {self.login_handler.websocket_client}")
            except Exception as condition_ex:
                logging.error(f"❌ 조건검색 목록조회 실패: {condition_ex}")
                import traceback
                logging.error(f"조건검색 목록조회 예외 상세: {traceback.format_exc()}")

            # 4. 자동매매 객체 초기화
            if not self.autotrader:
                self.autotrader = AutoTrader(self.trader, self)
                logging.debug("🔍 AutoTrader 객체 생성 완료")

            # 5. 차트 데이터 캐시 초기화
            try:
                if not self.chart_cache:
                    self.chart_cache = ChartDataCache(self.trader, self)
                    logging.debug("🔍 ChartDataCache 객체 생성 완료")
                if hasattr(self.login_handler, 'kiwoom_client') and self.login_handler.kiwoom_client:
                    self.login_handler.kiwoom_client.chart_cache = self.chart_cache
                    logging.debug("🔍 chart_cache를 KiwoomRestClient에 설정 완료")
                logging.info("✅ 차트 데이터 캐시 초기화 완료")
            except Exception as cache_ex:
                logging.error(f"❌ 차트 데이터 캐시 초기화 실패: {cache_ex}")
                import traceback
                logging.error(f"차트 캐시 초기화 예외 상세: {traceback.format_exc()}")
                self.chart_cache = None

            # 6. 시그널 연결
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
                import traceback
                logging.error(f"시그널 연결 예외 상세: {traceback.format_exc()}")

            # 7. 계좌 잔고조회 (2초 후 실행)
            try:
                import asyncio
                async def delayed_balance_query():
                    await asyncio.sleep(2.0)  # 2초 대기
                    self.handle_acnt_balance_query()
                asyncio.create_task(delayed_balance_query())
                logging.debug("⏰ 계좌 잔고조회 예약 (2초 후 실행)")
            except Exception as balance_ex:
                logging.error(f"❌ 계좌 잔고조회 타이머 설정 실패: {balance_ex}")
                import traceback
                logging.error(f"잔고조회 타이머 예외 상세: {traceback.format_exc()}")

            # 8. 모니터링 종목 코드 추출 (6초 후 실행)
            try:
                import asyncio
                async def delayed_extract_monitoring():
                    await asyncio.sleep(6.0)  # 6초 대기
                    self.extract_monitoring_stock_codes()
                asyncio.create_task(delayed_extract_monitoring())
                logging.debug("⏰ 모니터링 종목 코드 추출 예약 (6초 후 실행)")
            except Exception as timer_ex:
                logging.error(f"❌ 모니터링 종목 추출 타이머 설정 실패: {timer_ex}")
                import traceback
                logging.error(f"타이머 설정 예외 상세: {traceback.format_exc()}")
            
            logging.info("🔧 초기화 완료 - REST API와 웹소켓이 분리되어 관리됩니다.")

        except Exception as ex:
            logging.error(f"❌ 로그인 후 초기화 실패: {ex}")
            import traceback
            logging.error(f"초기화 실패 예외 상세: {traceback.format_exc()}")
            logging.info("⚠️ 초기화 실패했지만 프로그램을 계속 실행합니다")
    
    def update_acnt_balance_display(self, balance_data):
        """잔고 정보 표시 업데이트"""
        try:
            total_assets = balance_data.get('total_assets', 0)
            holdings_count = balance_data.get('holdings_count', 0)
            
            balance_text = f"총 자산: {total_assets:,}원\n"
            balance_text += f"보유 종목: {holdings_count}개"
            
            # balanceLabel이 존재하는 경우에만 업데이트
            if hasattr(self, 'balanceLabel') and self.balanceLabel:
                self.balanceLabel.setText(balance_text)
            else:
                # balanceLabel이 없는 경우 로그로만 출력
                logging.info(f"잔고 정보: {balance_text}")
            
        except Exception as ex:
            logging.error(f"잔고 정보 업데이트 실패: {ex}")
    
    def update_order_result(self, code, order_type, quantity, price, success):
        """주문 결과 업데이트"""
        try:
            status = "성공" if success else "실패"
            action = "매수" if order_type == "buy" else "매도"
            
            message = f"{action} 주문 {status}: {code} {quantity}주 @ {price}"
            
            if success:
                logging.info(message)
                
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
            logging.info(message)
            
        except Exception as ex:
            logging.error(f"전략 결과 업데이트 실패: {ex}")
    
    def closeEvent(self, event):
        """윈도우 종료 이벤트"""
        try:
            # 현재 선택된 투자전략을 settings.ini에 저장
            self.save_current_strategy()
            
            # 자동매매 중지
            if self.autotrader:
                self.autotrader.stop_auto_trading()
            
            # 차트 관련 정리
            if hasattr(self, 'chartdrawer') and self.chartdrawer:
                try:
                    logging.info("📊 ChartDrawer 정리 시작")
                    # ChartDrawer의 처리 상태 초기화
                    if hasattr(self.chartdrawer, '_processing_code'):
                        self.chartdrawer._processing_code = None
                    
                    # ChartDrawer 처리 상태 정리
                    if hasattr(self.chartdrawer, '_is_processing'):
                        self.chartdrawer._is_processing = False
                        self.chartdrawer._processing_code = None
                    
                    # ChartDrawer 참조 제거
                    self.chartdrawer = None
                    logging.info("✅ ChartDrawer 정리 완료")
                except Exception as drawer_ex:
                    logging.error(f"❌ ChartDrawer 정리 실패: {drawer_ex}")
            
            # WebEngine 정리
            if hasattr(self, 'chart_browser') and self.chart_browser:
                try:
                    logging.info("🌐 WebEngine 정리 시작")
                    # WebEngine 페이지 정리
                    if hasattr(self.chart_browser, 'page'):
                        self.chart_browser.page().deleteLater()
                    
                    # WebEngine 프로필 정리
                    if hasattr(self.chart_browser, 'page'):
                        try:
                            profile = self.chart_browser.page().profile()
                            if profile:
                                # 프로필 정리
                                profile.clearHttpCache()
                                profile.clearAllVisitedLinks()
                                logging.info("✅ WebEngine 프로필 캐시 정리 완료")
                        except Exception as profile_ex:
                            logging.warning(f"⚠️ WebEngine 프로필 정리 실패: {profile_ex}")
                    
                    # WebEngine 브라우저 정리
                    self.chart_browser.setParent(None)
                    self.chart_browser.deleteLater()
                    self.chart_browser = None
                    logging.info("✅ WebEngine 정리 완료")
                except Exception as webengine_ex:
                    logging.error(f"❌ WebEngine 정리 실패: {webengine_ex}")
            
            # 차트 데이터 캐시 정리
            if hasattr(self, 'chart_cache') and self.chart_cache:
                try:
                    logging.info("📊 차트 데이터 캐시 정리 시작")
                    self.chart_cache.stop()
                    logging.info("✅ 차트 데이터 캐시 정리 완료")
                except Exception as cache_ex:
                    logging.error(f"❌ 차트 데이터 캐시 정리 실패: {cache_ex}")
            
            
            # 웹소켓 클라이언트 종료
            if hasattr(self, 'login_handler') and self.login_handler:
                try:
                    logging.info("🔌 웹소켓 클라이언트 종료 시작")
                    if hasattr(self.login_handler, 'websocket_client') and self.login_handler.websocket_client:
                        # 웹소켓 연결 종료
                        self.login_handler.websocket_client.keep_running = False
                        self.login_handler.websocket_client.connected = False
                        
                        # 웹소켓 태스크 취소
                        if hasattr(self.login_handler, 'websocket_task') and self.login_handler.websocket_task:
                            self.login_handler.websocket_task.cancel()
                            logging.info("✅ 웹소켓 태스크 취소 완료")
                        
                        # 웹소켓 연결 강제 종료
                        try:
                            import asyncio
                            loop = asyncio.get_event_loop()
                            if loop and not loop.is_closed():
                                # 비동기 disconnect 호출
                                asyncio.create_task(self.login_handler.websocket_client.disconnect())
                                logging.info("✅ 웹소켓 비동기 연결 해제 완료")
                        except Exception as async_ex:
                            logging.warning(f"⚠️ 웹소켓 비동기 연결 해제 실패: {async_ex}")
                    
                    logging.info("✅ 웹소켓 클라이언트 종료 완료")
                except Exception as ws_ex:
                    logging.error(f"❌ 웹소켓 클라이언트 종료 실패: {ws_ex}")
                    import traceback
                    logging.error(f"웹소켓 종료 에러 상세: {traceback.format_exc()}")
            
            # 키움 클라이언트 연결 해제
            if self.trader and self.trader.client:
                try:
                    logging.info("🔌 키움 클라이언트 연결 해제 시작")
                    self.trader.client.disconnect()
                    logging.info("✅ 키움 클라이언트 연결 해제 완료")
                except Exception as disconnect_ex:
                    logging.error(f"❌ 키움 클라이언트 연결 해제 실패: {disconnect_ex}")
                    import traceback
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
                    
                    # QTextEdit 위젯 정리 (핸들러 제거 후)
                    try:
                        self.terminalOutput.clear()
                        self.terminalOutput.setParent(None)
                        self.terminalOutput = None
                    except (RuntimeError, AttributeError):
                        # 위젯이 이미 삭제된 경우 무시
                        pass
                        
                except Exception as e:
                    # QTextEdit 정리 실패 시 무시 (프로그램 종료 중이므로)
                    pass
            
            # 모든 타이머 정리
            try:
                from PyQt6.QtCore import QTimer
                # 모든 활성 타이머 정리
                for timer in self.findChildren(QTimer):
                    if timer.isActive():
                        timer.stop()
                logging.info("✅ 모든 타이머 정리 완료")
            except Exception as timer_ex:
                logging.error(f"❌ 타이머 정리 실패: {timer_ex}")
            
            # asyncio 이벤트 루프 정리
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop and not loop.is_closed():
                    # 모든 태스크 취소
                    tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
                    if tasks:
                        for task in tasks:
                            task.cancel()
                        # 취소된 태스크들 완료 대기
                        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                    logging.info("✅ asyncio 이벤트 루프 정리 완료")
            except Exception as asyncio_ex:
                logging.error(f"❌ asyncio 정리 실패: {asyncio_ex}")
            
            # Qt 애플리케이션 정리
            try:
                from PyQt6.QtWidgets import QApplication
                from PyQt6.QtCore import QCoreApplication
                
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
                    logging.info("✅ Qt 위젯 정리 완료")
            except Exception as qt_ex:
                logging.error(f"❌ Qt 정리 실패: {qt_ex}")
            
            # 가비지 컬렉션 실행
            gc.collect()
            
            logging.info("✅ 프로그램 종료 처리 완료")
            event.accept()
            
        except Exception as ex:
            logging.error(f"윈도우 종료 처리 실패: {ex}")
            event.accept()
    
    # ==================== 조건검색 관련 메서드 ====================
    def load_condition_list(self):
        """조건검색식 목록을 투자전략 콤보박스에 추가"""
        try:
            logging.info("🔍 조건검색식 목록 로드 시작")
            logging.info("📋 조건검색은 웹소켓을 통해서만 작동합니다")
            
            # 키움 클라이언트 참조 확인
            kiwoom_client = None
            if hasattr(self, 'login_handler') and hasattr(self.login_handler, 'kiwoom_client'):
                kiwoom_client = self.login_handler.kiwoom_client
            elif hasattr(self, 'kiwoom_client'):
                kiwoom_client = self.kiwoom_client
            
            if not kiwoom_client:
                logging.warning("⚠️ 키움 클라이언트가 초기화되지 않았습니다")
                self.update_condition_status("실패")
                return
            
            # 웹소켓 연결 상태 확인
            websocket_connected = False
            if hasattr(self.login_handler, 'websocket_client') and self.login_handler.websocket_client:
                websocket_connected = self.login_handler.websocket_client.connected
                logging.info(f"🔍 웹소켓 연결 상태: {websocket_connected}")
            
            if not websocket_connected:
                logging.warning("⚠️ 웹소켓이 연결되지 않았습니다.")
                self.update_condition_status("웹소켓 미연결")
                return
            
            logging.info("✅ 웹소켓 연결 상태 확인 완료")
            logging.info("🔍 웹소켓을 통한 조건검색식 목록 조회 시작")
            
            # 웹소켓을 통해 받은 조건검색 목록 사용
            if not hasattr(self, 'condition_search_list') or not self.condition_search_list:
                logging.warning("⚠️ 웹소켓을 통해 받은 조건검색 목록이 없습니다")
                self.update_condition_status("목록 없음")
                return
            
            # 웹소켓으로 받은 조건검색 목록을 변환
            condition_list = []
            for condition in self.condition_search_list:
                seq = condition['seq']
                name = condition['title']
                condition_list.append((seq, name))
            
            if condition_list:
                self.condition_list = condition_list
                logging.info(f"📋 조건검색식 목록 조회 성공: {len(condition_list)}개")
                
                # 투자전략 콤보박스에 조건검색식 추가
                added_count = 0
                for seq, name in condition_list:
                    condition_text = name  # [조건검색] 접두사 제거
                    self.comboStg.addItem(condition_text)
                    added_count += 1
                    logging.info(f"✅ 조건검색식 추가 ({added_count}/{len(condition_list)}): {condition_text}")
                
                logging.info(f"✅ 조건검색식 목록 로드 완료: {len(condition_list)}개 종목이 투자전략 콤보박스에 추가됨")
                logging.info("📋 이제 투자전략 콤보박스에서 조건검색식을 선택할 수 있습니다")
                
                # 조건검색식 로드 후 저장된 조건검색식이 있는지 확인하고 자동 실행
                logging.info("🔍 저장된 조건검색식 자동 실행 확인 시작")
                self.check_and_auto_execute_saved_condition()
                
            else:
                logging.warning("⚠️ 조건검색식 목록이 비어있습니다")
                logging.info("📋 키움증권 HTS에서 조건검색식을 먼저 생성하세요")
                self.update_condition_status("목록 없음")
                
        except Exception as ex:
            logging.error(f"❌ 조건검색식 목록 로드 실패: {ex}")
            import traceback
            logging.error(f"조건검색식 목록 로드 에러 상세: {traceback.format_exc()}")
            self.update_condition_status("실패")

    def check_and_auto_execute_saved_condition(self):
        """저장된 조건검색식이 있는지 확인하고 자동 실행"""
        try:
            logging.info("🔍 저장된 조건검색식 확인 시작")
            
            # settings.ini에서 저장된 전략 확인
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            if config.has_option('SETTINGS', 'last_strategy'):
                last_strategy = config.get('SETTINGS', 'last_strategy')
                logging.info(f"📋 저장된 전략 확인: {last_strategy}")
                
                # 저장된 전략이 조건검색식인지 확인 (조건검색 목록에 있는지 확인)
                if hasattr(self, 'condition_search_list') and self.condition_search_list:
                    condition_names = [condition['title'] for condition in self.condition_search_list]
                    if last_strategy in condition_names:
                        logging.info(f"🔍 저장된 조건검색식 발견: {last_strategy}")
                        
                        # 콤보박스에서 해당 조건검색식 찾기
                        index = self.comboStg.findText(last_strategy)
                        if index >= 0:
                            # 조건검색식 선택
                            self.comboStg.setCurrentIndex(index)
                            logging.info(f"✅ 저장된 조건검색식 선택: {last_strategy}")
                            
                            # 자동 실행 (1초 후)
                            import asyncio
                            async def delayed_condition_search():
                                await asyncio.sleep(1.0)  # 1초 대기
                                await self.handle_condition_search()
                            asyncio.create_task(delayed_condition_search())
                            logging.info("🔍 저장된 조건검색식 자동 실행 예약 (1초 후)")
                            logging.info("📋 조건검색식이 자동으로 실행되어 모니터링 종목에 추가됩니다")
                            return True  # 저장된 조건검색식 실행됨
                
                # 통합 전략인 경우 모든 조건검색식 실행
                if last_strategy == "통합 전략":
                    logging.info(f"🔍 저장된 통합 전략 발견: {last_strategy}")
                    
                    # 콤보박스에서 통합 전략 찾기
                    index = self.comboStg.findText(last_strategy)
                    if index >= 0:
                        # 통합 전략 선택
                        self.comboStg.setCurrentIndex(index)
                        logging.info(f"✅ 저장된 통합 전략 선택: {last_strategy}")
                        
                        # 자동 실행 (1초 후)
                        import asyncio
                        async def delayed_integrated_search():
                            await asyncio.sleep(1.0)  # 1초 대기
                            await self.handle_integrated_condition_search()
                        asyncio.create_task(delayed_integrated_search())
                        logging.info("🔍 저장된 통합 전략 자동 실행 예약 (1초 후)")
                        logging.info("📋 모든 조건검색식이 자동으로 실행되어 모니터링 종목에 추가됩니다")
                        return True  # 저장된 통합 전략 실행됨
                    else:
                        logging.warning(f"⚠️ 저장된 조건검색식을 콤보박스에서 찾을 수 없습니다: {last_strategy}")
                        logging.info("📋 조건검색식 목록을 다시 확인하거나 수동으로 선택하세요")
                        return False  # 저장된 조건검색식이 콤보박스에 없음
                else:
                    logging.info(f"📋 저장된 전략이 조건검색식이 아닙니다: {last_strategy}")
                    logging.info("📋 일반 투자전략이 선택되어 있습니다")
                    return False  # 조건검색식이 아님
            else:
                logging.info("📋 저장된 전략이 없습니다")
                logging.info("📋 투자전략 콤보박스에서 원하는 전략을 선택하세요")
                return False  # 저장된 전략이 없음
                
        except Exception as ex:
            logging.error(f"❌ 저장된 조건검색식 확인 및 자동 실행 실패: {ex}")
            import traceback
            logging.error(f"저장된 조건검색식 확인 에러 상세: {traceback.format_exc()}")
            return False  # 오류 발생

    async def handle_condition_search(self):
        """조건검색 버튼 클릭 처리"""
        try:
            current_text = self.comboStg.currentText()
            logging.info(f"🔍 조건검색 실행 요청: {current_text}")
            
            # 조건검색식인지 확인 (조건검색 목록에 있는지 확인)
            if not hasattr(self, 'condition_search_list') or not self.condition_search_list:
                logging.warning("⚠️ 조건검색 목록이 없습니다")
                return
            
            condition_names = [condition['title'] for condition in self.condition_search_list]
            if current_text not in condition_names:
                logging.warning("⚠️ 선택된 항목이 조건검색식이 아닙니다")
                logging.info(f"📋 사용 가능한 조건검색식: {condition_names}")
                return
            
            # 키움 클라이언트 참조 확인
            kiwoom_client = None
            if hasattr(self, 'login_handler') and hasattr(self.login_handler, 'kiwoom_client'):
                kiwoom_client = self.login_handler.kiwoom_client
            elif hasattr(self, 'kiwoom_client'):
                kiwoom_client = self.kiwoom_client
            
            if not kiwoom_client:
                logging.error("❌ 키움 클라이언트가 초기화되지 않았습니다")
                self.update_condition_status("실패")
                return
            
            # 웹소켓 연결 상태 확인
            websocket_connected = False
            if hasattr(self.login_handler, 'websocket_client') and self.login_handler.websocket_client:
                websocket_connected = self.login_handler.websocket_client.connected
                logging.info(f"🔍 웹소켓 연결 상태: {websocket_connected}")
            
            if not websocket_connected:
                logging.error("❌ 웹소켓이 연결되지 않았습니다.")
                self.update_condition_status("웹소켓 미연결")
                return
            
            logging.info("✅ 웹소켓 연결 상태 확인 완료")
            
            # 조건검색식 이름에서 일련번호 찾기
            condition_name = current_text  # [조건검색] 접두사가 이미 제거됨
            condition_seq = None
            
            logging.info(f"🔍 조건검색식 일련번호 검색: {condition_name}")
            for seq, name in self.condition_list:
                if name == condition_name:
                    condition_seq = seq
                    break
            
            if not condition_seq:
                logging.error(f"❌ 조건검색식 일련번호를 찾을 수 없습니다: {condition_name}")
                logging.info("📋 조건검색식 목록을 다시 로드하거나 키움증권 HTS에서 확인하세요")
                return
            
            logging.info(f"✅ 조건검색식 일련번호 확인: {condition_name} (seq: {condition_seq})")
            logging.info("🔍 조건검색 실행 시작")
            logging.info("📋 조건검색은 웹소켓을 통해 일반 검색과 실시간 검색을 모두 실행합니다")
            
            # 조건검색 상태를 실행중으로 업데이트
            self.update_condition_status("실행중")
            
            # 조건검색 일반 요청으로 종목 추출
            logging.info("🔍 조건검색 일반 요청 시작")
            await self.search_condition_normal(condition_seq)
            
            # 조건검색 실시간 요청으로 지속적 모니터링 시작
            logging.info("🔍 조건검색 실시간 요청 시작")
            await self.start_condition_realtime(condition_seq)
            
            # 조건검색 상태를 완료로 업데이트
            self.update_condition_status("완료")
            logging.info("✅ 조건검색 실행 완료")
            logging.info("📋 조건검색 결과가 모니터링 종목에 추가되었습니다")
            
        except Exception as ex:
            logging.error(f"❌ 조건검색 처리 실패: {ex}")
            import traceback
            logging.error(f"조건검색 처리 에러 상세: {traceback.format_exc()}")
            # 조건검색 상태를 실패로 업데이트
            self.update_condition_status("실패")

    async def handle_integrated_condition_search(self):
        """통합 전략: 모든 조건검색식 실행"""
        try:
            logging.info("🔍 통합 조건검색 실행 시작")
            
            if not hasattr(self, 'condition_search_list') or not self.condition_search_list:
                logging.warning("⚠️ 조건검색 목록이 없습니다")
                return
            
            # 모든 조건검색식 실행
            for condition in self.condition_search_list:
                condition_name = condition['title']
                condition_seq = condition['seq']
                
                logging.info(f"🔍 조건검색 실행: {condition_name} (seq: {condition_seq})")
                
                # 조건검색 일반 요청으로 종목 추출
                await self.search_condition_normal(condition_seq)
                
                # 잠시 대기 (서버 부하 방지)
                import asyncio
                await asyncio.sleep(0.5)
            
            logging.info("✅ 통합 조건검색 실행 완료")
            logging.info("📋 모든 조건검색 결과가 모니터링 종목에 추가되었습니다")
            
        except Exception as ex:
            logging.error(f"❌ 통합 조건검색 처리 실패: {ex}")
            import traceback
            logging.error(f"통합 조건검색 처리 에러 상세: {traceback.format_exc()}")

    async def search_condition_normal(self, seq):
        """조건검색 일반 요청으로 종목 추출하여 모니터링 종목에 추가 (웹소켓 기반)"""
        try:
            # 웹소켓 클라이언트 확인
            if not hasattr(self.login_handler, 'websocket_client') or not self.login_handler.websocket_client:
                logging.error("❌ 웹소켓 클라이언트가 연결되지 않았습니다")
                return
            
            if not self.login_handler.websocket_client.connected:
                logging.error("❌ 웹소켓이 연결되지 않았습니다")
                return
            
            logging.info(f"🔍 조건검색 일반 요청 (웹소켓): {seq}")
            
            # 웹소켓을 통한 조건검색 일반 요청 (예시코드 방식)
            await self.login_handler.websocket_client.send_message({
                'trnm': 'CNSRREQ',  # 조건검색 일반 요청 TR명 (예시코드 방식)
                'seq': seq,
                'search_type': '0',  # 조회타입
                'stex_tp': 'K',  # 거래소구분
                'cont_yn': 'N',  # 연속조회여부
                'next_key': ''  # 연속조회키
            })
            
            logging.info(f"✅ 조건검색 일반 요청 전송 완료 (웹소켓): {seq}")
            # 응답은 웹소켓에서 처리됨
            logging.info(f"💾 조건검색 일반 요청 완료 - 응답은 웹소켓에서 처리됩니다: {seq}")
                
        except Exception as ex:
            logging.error(f"❌ 조건검색 일반 요청 실패: {ex}")
            import traceback
            logging.error(f"조건검색 일반 요청 에러 상세: {traceback.format_exc()}")
            self.update_condition_status("실패")

    async def start_condition_realtime(self, seq):
        """조건검색 실시간 요청으로 지속적 모니터링 시작 (웹소켓 기반)"""
        try:
            # 웹소켓 클라이언트 확인
            if not hasattr(self.login_handler, 'websocket_client') or not self.login_handler.websocket_client:
                logging.error("❌ 웹소켓 클라이언트가 연결되지 않았습니다")
                return
            
            if not self.login_handler.websocket_client.connected:
                logging.error("❌ 웹소켓이 연결되지 않았습니다")
                return
            
            logging.info(f"🔍 조건검색 실시간 요청 시작 (웹소켓): {seq}")
            
            # 웹소켓을 통한 조건검색 실시간 요청 (예시코드 방식)
            await self.login_handler.websocket_client.send_message({
                'trnm': 'CNSRREQ',  # 조건검색 실시간 요청 TR명 (예시코드 방식)
                'seq': seq,
                'search_type': '1',  # 조회타입 (실시간)
                'stex_tp': 'K'  # 거래소구분
            })
            
            logging.info(f"✅ 조건검색 실시간 요청 전송 완료 (웹소켓): {seq}")
            # 응답은 웹소켓에서 처리됨
            logging.info(f"💾 조건검색 실시간 요청 완료 - 응답은 웹소켓에서 처리됩니다: {seq}")
                
        except Exception as ex:
            logging.error(f"❌ 조건검색 실시간 요청 실패: {ex}")
            import traceback
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
            
            logging.info(f"🔍 조건검색 실시간 해제 (웹소켓): {seq}")
            
            # 웹소켓을 통한 조건검색 실시간 해제
            await self.login_handler.websocket_client.send_message({
                'trnm': 'CNSCLR',  # 조건검색 실시간 해제 TR명
                'seq': seq
            })
            
            logging.info(f"✅ 조건검색 실시간 해제 전송 완료 (웹소켓): {seq}")
            # 응답은 웹소켓에서 처리됨
            logging.info(f"💾 조건검색 실시간 해제 완료 - 응답은 웹소켓에서 처리됩니다: {seq}")
                
        except Exception as ex:
            logging.error(f"❌ 조건검색 실시간 해제 실패: {ex}")
            import traceback
            logging.error(f"조건검색 실시간 해제 에러 상세: {traceback.format_exc()}")

    def remove_condition_stocks_from_monitoring(self, seq):
        """조건검색으로 추가된 종목들을 모니터링에서 제거"""
        try:
            if seq not in self.condition_search_results:
                logging.warning(f"⚠️ 조건검색 결과를 찾을 수 없습니다: {seq}")
                return
            
            condition_data = self.condition_search_results[seq]
            removed_count = 0
            
            # 일반 검색 결과에서 제거
            if 'normal_results' in condition_data:
                for item in condition_data['normal_results']:
                    if len(item) >= 1:
                        code = item[0]
                        if self.remove_stock_from_monitoring(code):
                            removed_count += 1
            
            # 실시간 검색 결과에서 제거
            if 'realtime_results' in condition_data:
                for item in condition_data['realtime_results']:
                    if len(item) >= 1:
                        code = item[0]
                        if self.remove_stock_from_monitoring(code):
                            removed_count += 1
            
            logging.info(f"✅ 조건검색 종목 제거 완료: {removed_count}개 종목을 모니터링에서 제거")
            
            # 조건검색 결과에서 제거
            del self.condition_search_results[seq]
            
        except Exception as ex:
            logging.error(f"❌ 조건검색 종목 제거 실패: {ex}")
            import traceback
            logging.error(f"조건검색 종목 제거 에러 상세: {traceback.format_exc()}")

    def get_monitoring_codes(self):
        """현재 모니터링 박스에 있는 종목 코드들을 반환"""
        try:
            codes = []
            for i in range(self.monitoringBox.count()):
                item_text = self.monitoringBox.item(i).text()
                # "종목코드 - 종목명" 형식에서 종목코드만 추출
                if ' - ' in item_text:
                    code = item_text.split(' - ')[0]
                    codes.append(code)
            return codes
        except Exception as ex:
            logging.error(f"❌ 모니터링 종목 코드 조회 실패: {ex}")
            return []

    def add_stock_to_monitoring(self, code, name):
        """종목을 모니터링 목록에 추가"""
        try:
            # 이미 존재하는지 확인
            for i in range(self.monitoringBox.count()):
                item_text = self.monitoringBox.item(i).text()
                if code in item_text:
                    logging.debug(f"종목이 이미 모니터링에 존재합니다: {code} - {name}")
                    return False
            
            # 모니터링 목록에 추가
            item_text = f"{code} - {name}"
            self.monitoringBox.addItem(item_text)
            
            logging.info(f"✅ 모니터링 종목 추가: {item_text}")
            
            # 차트 캐시에도 추가
            if hasattr(self, 'chart_cache') and self.chart_cache:
                self.chart_cache.add_monitoring_stock(code)
            
            
            return True
            
        except Exception as ex:
            logging.error(f"❌ 모니터링 종목 추가 실패: {ex}")
            return False

    def add_stock_to_monitoring_realtime(self, code, name):
        """조건검색 실시간으로 종목을 모니터링 목록에 추가"""
        try:
            # 이미 존재하는지 확인
            for i in range(self.monitoringBox.count()):
                item_text = self.monitoringBox.item(i).text()
                if code in item_text:
                    logging.debug(f"종목이 이미 모니터링에 존재합니다: {code} - {name}")
                    return False
            
            # 모니터링 목록에 추가
            item_text = f"{code} - {name}"
            self.monitoringBox.addItem(item_text)
            
            logging.info(f"✅ 실시간 모니터링 종목 추가: {item_text}")
            
            # 차트 캐시에도 추가
            if hasattr(self, 'chart_cache') and self.chart_cache:
                self.chart_cache.add_monitoring_stock(code)
            
            # 웹소켓 기능이 제거됨 - 별도로 관리됨
            
            return True
            
        except Exception as ex:
            logging.error(f"❌ 실시간 모니터링 종목 추가 실패: {ex}")
            return False


    def remove_stock_from_monitoring(self, code):
        """종목을 모니터링 목록에서 제거"""
        try:
            for i in range(self.monitoringBox.count()):
                item_text = self.monitoringBox.item(i).text()
                if code in item_text:
                    self.monitoringBox.takeItem(i)
                    logging.info(f"✅ 모니터링 종목 제거: {item_text}")
                    
                    # 차트 캐시에서도 제거
                    if hasattr(self, 'chart_cache') and self.chart_cache:
                        self.chart_cache.remove_monitoring_stock(code)
                    
                    # 웹소켓 기능이 제거됨 - 별도로 관리됨
                    
                    return True
            
            logging.debug(f"모니터링에서 제거할 종목을 찾을 수 없습니다: {code}")
            return False
            
        except Exception as ex:
            logging.error(f"❌ 모니터링 종목 제거 실패: {ex}")
            return False


    def handle_condition_realtime_update(self, seq, action, stock_list):
        """조건검색 실시간 업데이트 처리 (UI 스레드 안전)"""
        try:
            # UI 스레드에서 실행되는지 확인
            if not QThread.isMainThread():
                logging.warning("handle_condition_realtime_update가 메인 스레드가 아닌 곳에서 호출됨")
                return
            
            logging.info(f"🔍 조건검색 실시간 업데이트: seq={seq}, action={action}, stocks={len(stock_list)}개")
            
            # 활성화된 실시간 조건검색인지 확인
            if seq not in self.active_realtime_conditions:
                logging.warning(f"⚠️ 비활성화된 조건검색 실시간 알림: {seq}")
                return
            
            if action == 'ADD' or action == 'add':
                # 새로운 종목들이 조건에 맞아서 추가됨
                added_count = 0
                for stock_item in stock_list:
                    if len(stock_item) >= 2:
                        code = stock_item[0]  # 종목코드
                        name = stock_item[1]  # 종목명
                        
                        # 모니터링 종목에 추가 (개별 웹소켓 구독)
                        if self.add_stock_to_monitoring_realtime(code, name):
                            added_count += 1
                
                logging.info(f"✅ 조건검색 실시간 추가: {added_count}개 종목을 모니터링에 추가")
                
            elif action == 'REMOVE' or action == 'remove':
                # 기존 종목들이 조건에서 벗어나서 제거됨
                removed_count = 0
                for stock_item in stock_list:
                    if len(stock_item) >= 1:
                        code = stock_item[0]  # 종목코드
                        
                        # 모니터링 종목에서 제거
                        if self.remove_stock_from_monitoring(code):
                            removed_count += 1
                
                logging.info(f"✅ 조건검색 실시간 제거: {removed_count}개 종목을 모니터링에서 제거")
                
            else:
                logging.warning(f"⚠️ 알 수 없는 조건검색 실시간 액션: {action}")
                
        except Exception as ex:
            logging.error(f"❌ 조건검색 실시간 업데이트 처리 실패: {ex}")
            import traceback
            logging.error(f"조건검색 실시간 업데이트 처리 에러 상세: {traceback.format_exc()}")

    def update_condition_status(self, status, count=None):
        """조건검색 상태 UI 업데이트"""
        try:
            # UI 라벨이 제거되었으므로 로그로만 상태 출력
            logging.info(f"조건검색 상태: {status}")
            if count is not None:
                logging.info(f"활성 조건검색 개수: {count}개")
                
        except Exception as ex:
            logging.error(f"❌ 조건검색 상태 업데이트 실패: {ex}")

    def save_current_strategy(self):
        """현재 선택된 투자전략을 settings.ini에 저장"""
        try:
            current_strategy = self.comboStg.currentText()
            if not current_strategy:
                logging.debug("저장할 투자전략이 없습니다")
                return
            
            # settings.ini 파일 읽기
            config = configparser.RawConfigParser()
            config.read('settings.ini', encoding='utf-8')
            
            # SETTINGS 섹션이 없으면 생성
            if not config.has_section('SETTINGS'):
                config.add_section('SETTINGS')
            
            # last_strategy 값 업데이트
            config.set('SETTINGS', 'last_strategy', current_strategy)
            
            # 파일에 저장
            with open('settings.ini', 'w', encoding='utf-8') as configfile:
                config.write(configfile)
            
            logging.info(f"✅ 투자전략 저장 완료: {current_strategy}")
            
        except Exception as ex:
            logging.error(f"❌ 투자전략 저장 실패: {ex}")
            import traceback
            logging.error(f"투자전략 저장 에러 상세: {traceback.format_exc()}")

# ==================== WebEngine 사용 가능 여부 확인 ====================
def check_webengine_availability():
    """WebEngine 사용 가능 여부를 확인하는 함수"""
    return WEBENGINE_AVAILABLE

# ==================== 메인 실행 ====================
async def main():
    """메인 실행 함수 - qasync를 사용한 비동기 처리"""
    try:
        print("🚀 프로그램 시작")
        
        # 로깅 설정
        setup_logging()
        logging.info("🚀 프로그램 시작 - 로깅 설정 완료")
        
        # qasync 애플리케이션 생성
        app = qasync.QApplication(sys.argv)
        
        # PyQt6-WebEngine 사용 가능 여부 확인
        webengine_available = WEBENGINE_AVAILABLE
        
        if webengine_available:
            logging.info("✅ PyQt6-WebEngine 사용 가능 - 인터랙티브 차트 지원")
        else:
            logging.warning("⚠️ PyQt6-WebEngine 사용 불가 - 기본 차트만 지원")
            logging.warning("⚠️ 인터랙티브 차트를 사용하려면 'pip install PyQt6-WebEngine' 실행")
        logging.info("✅ qasync 애플리케이션 생성 완료")
        
        # QApplication 생성 후 QTextCursor 메타타입 재등록
        register_qtextcursor_metatype()
        logging.info("✅ QTextCursor 메타타입 재등록 완료")
        
        # 메인 윈도우 생성
        window = MyWindow(webengine_available)
        logging.info("✅ 메인 윈도우 생성 완료")
        
        # 윈도우 표시
        window.show()
        logging.info("✅ 윈도우 표시 완료")
        
        # 애플리케이션 실행 (qasync에서는 이벤트 루프가 이미 실행 중)
        logging.info("🚀 애플리케이션 실행 시작")
        
        # qasync가 이벤트 루프를 관리하므로 여기서는 대기만 함
        # 웹소켓과 다른 비동기 작업들이 실행될 때까지 대기
        import asyncio
        try:
            # 이벤트 루프가 종료될 때까지 대기
            # CancelledError는 정상적인 종료 시그널이므로 예외로 처리하지 않음
            while True:
                await asyncio.sleep(0.1)  # 더 짧은 간격으로 변경
        except asyncio.CancelledError:
            # 정상적인 종료 시그널 - 예외로 처리하지 않음
            logging.info("✅ 프로그램 정상 종료")
            # QApplication 정리
            try:
                from PyQt6.QtWidgets import QApplication
                from PyQt6.QtCore import QCoreApplication
                
                app = QApplication.instance()
                if app:
                    # 모든 위젯 정리
                    for widget in app.allWidgets():
                        if widget.parent() is None:
                            widget.close()
                            widget.deleteLater()
                    
                    # 이벤트 처리 완료 대기
                    QCoreApplication.processEvents()
                    logging.info("✅ QApplication 정리 완료")
            except Exception as cleanup_ex:
                logging.error(f"❌ QApplication 정리 실패: {cleanup_ex}")
            return
        except KeyboardInterrupt:
            # Ctrl+C로 종료할 때
            logging.info("✅ 사용자에 의한 프로그램 종료")
            # QApplication 정리
            try:
                from PyQt6.QtWidgets import QApplication
                from PyQt6.QtCore import QCoreApplication
                
                app = QApplication.instance()
                if app:
                    # 모든 위젯 정리
                    for widget in app.allWidgets():
                        if widget.parent() is None:
                            widget.close()
                            widget.deleteLater()
                    
                    # 이벤트 처리 완료 대기
                    QCoreApplication.processEvents()
                    logging.info("✅ QApplication 정리 완료")
            except Exception as cleanup_ex:
                logging.error(f"❌ QApplication 정리 실패: {cleanup_ex}")
            return
        
    except Exception as ex:
        logging.error(f"❌ 메인 실행 실패: {ex}")
        import traceback
        logging.error(f"메인 실행 예외 상세: {traceback.format_exc()}")
        print(f"❌ 프로그램 실행 중 오류 발생: {ex}")
        print("프로그램을 종료합니다.")
        sys.exit(1)
    except BaseException as be:
        logging.error(f"❌ 메인 실행 중 예상치 못한 예외 발생: {be}")
        import traceback
        logging.error(f"예상치 못한 예외 상세: {traceback.format_exc()}")
        print(f"❌ 프로그램 실행 중 예상치 못한 오류 발생: {be}")
        print("프로그램을 종료합니다.")
        sys.exit(1)


# ==================== 차트 데이터 캐시 클래스 ====================
class ChartDataCache(QObject):
    """모니터링 종목 차트 데이터 메모리 캐시 클래스"""
    
    # 시그널 정의
    data_updated = pyqtSignal(str)  # 특정 종목 데이터 업데이트
    cache_cleared = pyqtSignal()    # 캐시 전체 정리
    
    def __init__(self, trader, parent=None):
        try:
            super().__init__(parent)            
            self.trader = trader            
            self.cache = {}  # {종목코드: {'tick_data': {}, 'min_data': {}, 'last_update': datetime}}
            logging.debug("🔍 캐시 딕셔너리 초기화 완료")
            
            # API 제한 관리자 및 스레드 관리
            self.api_limit_manager = ApiRequestManager()
            self.active_threads = {}  # 활성 스레드 관리 {종목코드: [tick_thread, minute_thread]}
            logging.debug("🔍 API 제한 관리자 초기화 완료")
            
            # QTimer 생성을 지연시켜 메인 스레드에서 실행되도록 함
            self.update_timer = None
            self.save_timer = None
            logging.debug("🔍 타이머 변수 초기화 완료")
            
            # API 시그널 연결
            self._connect_api_signals()
            
            # 메인 스레드에서 타이머 초기화 예약 (qasync 방식)
            import asyncio
            async def delayed_init_timers():
                await asyncio.sleep(0.1)  # 100ms 대기
                self._initialize_timers()
            asyncio.create_task(delayed_init_timers())
            logging.debug("🔍 타이머 초기화 예약 완료 (100ms 후)")
            
            logging.info("📊 차트 데이터 캐시 초기화 완료")
        except Exception as ex:
            logging.error(f"❌ ChartDataCache 초기화 실패: {ex}")
            import traceback
            logging.error(f"ChartDataCache 초기화 예외 상세: {traceback.format_exc()}")
            raise ex
    
    def _connect_api_signals(self):
        """API 제한 관리자 시그널 연결"""
        self.api_limit_manager.request_ready.connect(self._on_api_request_ready)
    
    def _on_api_request_ready(self, client, request_type, request_data):
        """API 요청 준비 시그널 처리"""
        code = request_data['code']
        kwargs = request_data['kwargs']
        
        # API 요청 스레드 생성 및 시작
        thread = ApiRequestThread(client, code, request_type, **kwargs)
        thread.data_ready.connect(lambda data: self._on_api_data_received(code, request_type, data))
        thread.error_occurred.connect(lambda error: self._on_api_error(code, request_type, error))
        thread.progress_updated.connect(self._on_api_progress)
        
        # 스레드 관리
        if code not in self.active_threads:
            self.active_threads[code] = []
        self.active_threads[code].append(thread)
        
        thread.start()
    
    def _on_api_data_received(self, code, request_type, data):
        """API 데이터 수신 시그널 처리"""
        try:
            if code not in self.cache:
                self.cache[code] = {}
            
            if request_type == 'tick':
                self.cache[code]['tick_data'] = data
                logging.info(f"✅ 틱 데이터 수신 완료: {code} - {len(data.get('close', []))}개")
            elif request_type == 'minute':
                self.cache[code]['min_data'] = data
                logging.info(f"✅ 분봉 데이터 수신 완료: {code} - {len(data.get('close', []))}개")
            
            # 데이터 업데이트 시그널 발생
            self.data_updated.emit(code)
            
        except Exception as ex:
            logging.error(f"❌ API 데이터 처리 실패 ({request_type}): {ex}")
        finally:
            # 스레드 정리
            self._cleanup_thread(code, request_type)
    
    def _on_api_error(self, code, request_type, error_msg):
        """API 에러 시그널 처리"""
        logging.error(f"❌ API 요청 실패 ({request_type}): {code} - {error_msg}")
        self._cleanup_thread(code, request_type)
    
    def _on_api_progress(self, progress_msg):
        """API 진행 상황 시그널 처리"""
        logging.info(f"📊 {progress_msg}")
    
    def _cleanup_thread(self, code, request_type):
        """완료된 스레드 정리"""
        if code in self.active_threads:
            # 해당 요청 타입의 스레드 제거
            self.active_threads[code] = [t for t in self.active_threads[code] 
                                       if not (hasattr(t, 'request_type') and t.request_type == request_type)]
            
            # 모든 스레드가 완료되면 종목 코드 제거
            if not self.active_threads[code]:
                del self.active_threads[code]
    
    def collect_chart_data_async(self, code, max_retries=3):
        """비동기 차트 데이터 수집 (UI 블로킹 방지)"""
        try:
            logging.info(f"🔧 비동기 차트 데이터 수집 시작: {code}")
            
            # 기존 스레드가 있으면 취소
            if code in self.active_threads:
                for thread in self.active_threads[code]:
                    thread.cancel()
                del self.active_threads[code]
            
            # 틱 데이터 요청 (즉시)
            self.api_limit_manager.request_with_delay(
                self.trader.client, code, 'tick', 
                delay_seconds=0, tic_scope=30, count=300
            )
            
            # 분봉 데이터 요청 (1초 후)
            self.api_limit_manager.request_with_delay(
                self.trader.client, code, 'minute', 
                delay_seconds=1.0, count=100
            )
            
            logging.info(f"✅ 비동기 차트 데이터 수집 요청 완료: {code}")
            
        except Exception as ex:
            logging.error(f"❌ 비동기 차트 데이터 수집 실패: {code} - {ex}")
    
    def collect_chart_data_legacy(self, code, max_retries=3):
        """기존 동기 방식 차트 데이터 수집 (호환성 유지)"""
        try:
            logging.info(f"🔧 레거시 차트 데이터 수집 시작: {code}")
            
            # 틱 데이터 수집
            tick_data = self._collect_tick_data_sync(code, max_retries)
            if not tick_data:
                logging.warning(f"⚠️ 틱 데이터가 None: {code}")
                return False
            
            # 분봉 데이터 수집
            min_data = self._collect_minute_data_sync(code, max_retries)
            if not min_data:
                logging.warning(f"⚠️ 분봉 데이터가 None: {code}")
                return False
            
            # 캐시에 저장
            if code not in self.cache:
                self.cache[code] = {}
            
            self.cache[code]['tick_data'] = tick_data
            self.cache[code]['min_data'] = min_data
            self.cache[code]['last_update'] = datetime.now()
            
            logging.info(f"✅ 레거시 차트 데이터 수집 완료: {code}")
            self.data_updated.emit(code)
            return True
            
        except Exception as ex:
            logging.error(f"❌ 레거시 차트 데이터 수집 실패: {code} - {ex}")
            return False
    
    def _collect_tick_data_sync(self, code, max_retries=3):
        """동기 방식 틱 데이터 수집"""
        for attempt in range(max_retries):
            try:
                # API 요청 간격 조정
                if attempt > 0:
                    wait_time = 2 ** attempt
                    logging.info(f"⏳ API 제한 대기 중... ({wait_time}초 후 재시도 {attempt + 1}/{max_retries})")
                    QTimer.singleShot(int(wait_time * 1000), lambda: None)  # QTimer로 대기
                
                logging.info(f"🔧 API 틱 데이터 조회 시작: {code} (시도 {attempt + 1}/{max_retries})")
                data = self.trader.client.get_stock_tick_chart(code, tic_scope=30, count=300)
                
                if data and data.get('close'):
                    logging.info(f"✅ 틱 데이터 조회 성공: {code} - 데이터 개수: {len(data['close'])}")
                    return data
                else:
                    logging.warning(f"⚠️ 틱 데이터가 비어있음: {code}")
                    
            except Exception as e:
                logging.error(f"❌ 틱 데이터 조회 실패: {code} (시도 {attempt + 1}/{max_retries}) - {e}")
                if attempt == max_retries - 1:
                    raise e
        
        return None
    
    def _collect_minute_data_sync(self, code, max_retries=3):
        """동기 방식 분봉 데이터 수집"""
        for attempt in range(max_retries):
            try:
                # API 요청 간격 조정
                if attempt > 0:
                    wait_time = 2 ** attempt
                    logging.info(f"⏳ API 제한 대기 중... ({wait_time}초 후 재시도 {attempt + 1}/{max_retries})")
                    QTimer.singleShot(int(wait_time * 1000), lambda: None)  # QTimer로 대기
                
                logging.info(f"🔧 API 분봉 데이터 조회 시작: {code} (시도 {attempt + 1}/{max_retries})")
                data = self.trader.client.get_stock_minute_chart(code, count=100)
                
                if data and data.get('close'):
                    logging.info(f"✅ 분봉 데이터 조회 성공: {code} - 데이터 개수: {len(data['close'])}")
                    return data
                else:
                    logging.warning(f"⚠️ 분봉 데이터가 비어있음: {code}")
                    
            except Exception as e:
                logging.error(f"❌ 분봉 데이터 조회 실패: {code} (시도 {attempt + 1}/{max_retries}) - {e}")
                if attempt == max_retries - 1:
                    raise e
        
        return None
    
    def _initialize_timers(self):
        """메인 스레드에서 타이머 초기화"""
        try:
            logging.debug("🔧 차트 데이터 캐시 타이머 초기화 시작 (메인 스레드)")
            logging.debug(f"🔍 현재 스레드: {threading.current_thread().name}")
            logging.debug(f"🔍 QThread 메인 스레드 여부: {QThread.isMainThread()}")
            
            # QTimer 생성 및 설정
            logging.debug("🔍 update_timer 생성 중...")
            self.update_timer = QTimer()
            logging.debug("🔍 update_timer timeout 시그널 연결 중...")
            self.update_timer.timeout.connect(self.update_all_charts)
            logging.debug("🔍 save_timer 생성 중...")
            self.save_timer = QTimer()
            logging.debug("🔍 save_timer timeout 시그널 연결 중...")
            self.save_timer.timeout.connect(self._trigger_async_save_to_database)
            
            # 타이머 시작
            logging.debug("🔍 update_timer 시작 중... (1분 간격)")
            self.update_timer.start(60000)    # 1분마다 차트 데이터 업데이트
            logging.debug("🔍 save_timer 시작 중... (60초 간격)")
            self.save_timer.start(60000)     # 60초마다 DB 저장
            
            logging.info("✅ 차트 데이터 캐시 타이머 초기화 완료")
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 캐시 타이머 초기화 실패: {ex}")
            import traceback
            logging.error(f"타이머 초기화 예외 상세: {traceback.format_exc()}")
    
    def add_monitoring_stock(self, code):
        """모니터링 종목 추가"""
        try:
            logging.info(f"🔧 모니터링 종목 추가 시도: {code}")
            
            if code not in self.cache:
                self.cache[code] = {
                    'tick_data': None,
                    'min_data': None,
                    'last_update': None,
                    'last_save': None
                }
                logging.info(f"✅ 모니터링 종목 추가 완료: {code}")
                
                # 즉시 데이터 수집
                logging.info(f"🔧 즉시 데이터 수집 시작: {code}")
                self.update_single_chart(code)
            else:
                logging.info(f"ℹ️ 모니터링 종목이 이미 존재함: {code}")
                
        except Exception as ex:
            logging.error(f"❌ 모니터링 종목 추가 실패 ({code}): {ex}")
            import traceback
            logging.error(f"종목 추가 예외 상세: {traceback.format_exc()}")
    
    def remove_monitoring_stock(self, code):
        """모니터링 종목 제거"""
        if code in self.cache:
            del self.cache[code]
            logging.info(f"📊 모니터링 종목 제거: {code}")
    
    def update_monitoring_stocks(self, codes):
        """모니터링 종목 리스트 업데이트"""
        try:
            logging.info(f"🔧 모니터링 종목 리스트 업데이트 시작")
            logging.info(f"새로운 종목 리스트: {codes}")
            
            current_codes = set(self.cache.keys())
            new_codes = set(codes)
            
            logging.info(f"현재 캐시된 종목: {list(current_codes)}")
            logging.info(f"새로운 종목: {list(new_codes)}")
            
            # 추가할 종목 (순차적으로 처리)
            to_add = new_codes - current_codes
            if to_add:
                logging.info(f"추가할 종목: {list(to_add)}")
                for i, code in enumerate(to_add):
                    logging.info(f"추가할 종목: {code} ({i+1}/{len(to_add)})")
                    self.add_monitoring_stock(code)
                    
                    # 마지막 종목이 아니면 잠시 대기 (API 제한 방지)
                    if i < len(to_add) - 1:
                        # QTimer를 사용한 비동기 대기 (UI 블로킹 방지)
                        QTimer.singleShot(1000, lambda: None)  # 1초 대기
                        logging.info(f"⏳ API 제한 방지를 위해 1초 대기 중...")
            
            # 제거할 종목
            to_remove = current_codes - new_codes
            if to_remove:
                logging.info(f"제거할 종목: {list(to_remove)}")
                for code in to_remove:
                    self.remove_monitoring_stock(code)
            
            # 모니터링 종목 변경 로그
            if new_codes:
                logging.info(f"✅ 모니터링 종목 변경 완료: {list(new_codes)}")
            else:
                logging.warning("⚠️ 모니터링 종목이 없습니다")
                
        except Exception as ex:
            logging.error(f"❌ 모니터링 종목 리스트 업데이트 실패: {ex}")
            import traceback
            logging.error(f"종목 리스트 업데이트 예외 상세: {traceback.format_exc()}")
    

    
    def update_single_chart(self, code):
        """단일 종목 차트 데이터 업데이트 (비동기)"""
        try:
            logging.info(f"🔧 차트 데이터 업데이트 시작: {code}")
            
            if not self.trader or not hasattr(self.trader, 'client') or not self.trader.client:
                logging.warning(f"⚠️ 트레이더 또는 클라이언트가 없음: {code}")
                return
            
            if not self.trader.client.is_connected:
                logging.warning(f"⚠️ API 연결되지 않음: {code}")
                return

            # 비동기 차트 데이터 수집 (UI 블로킹 방지)
            self.collect_chart_data_async(code)
            
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 업데이트 실패: {code} - {ex}")
    
    def update_single_chart_legacy(self, code):
        """단일 종목 차트 데이터 업데이트 (레거시 동기 방식)"""
        try:
            logging.info(f"🔧 레거시 차트 데이터 업데이트 시작: {code}")
            
            if not self.trader or not hasattr(self.trader, 'client') or not self.trader.client:
                logging.warning(f"⚠️ 트레이더 또는 클라이언트가 없음: {code}")
                return
            
            if not self.trader.client.is_connected:
                logging.warning(f"⚠️ API 연결되지 않음: {code}")
                return

            logging.info(f"🔧 틱 데이터 수집 시작: {code}")
            # 틱 데이터 수집
            tick_data = self.get_tick_data_from_api(code)
            logging.info(f"🔧 틱 데이터 수집 완료: {code} - 데이터 존재: {tick_data is not None}")
            
            logging.info(f"🔧 분봉 데이터 수집 시작: {code}")
            min_data = self.get_min_data_from_api(code)
            logging.info(f"🔧 분봉 데이터 수집 완료: {code} - 데이터 존재: {min_data is not None}")
            
            # 부분적 성공 허용: 틱 데이터 또는 분봉 데이터 중 하나라도 있으면 저장
            if tick_data or min_data:
                self.cache[code] = {
                    'tick_data': tick_data,
                    'min_data': min_data,
                    'last_update': datetime.now(),
                    'last_save': self.cache.get(code, {}).get('last_save')
                }
                
                if tick_data and min_data:
                    logging.info(f"✅ 차트 데이터 업데이트 완료: {code} (틱+분봉)")
                    # 차트 데이터 저장 시 해당 종목 분석표 출력
                    logging.info(f"🔧 분석표 출력 시작: {code}")
                    self.log_single_stock_analysis(code, tick_data, min_data)
                    logging.info(f"✅ 분석표 출력 완료: {code}")
                elif tick_data:
                    logging.info(f"✅ 차트 데이터 업데이트 완료: {code} (틱 데이터만)")
                    logging.warning(f"⚠️ 분봉 데이터 없음: {code}")
                elif min_data:
                    logging.info(f"✅ 차트 데이터 업데이트 완료: {code} (분봉 데이터만)")
                    logging.warning(f"⚠️ 틱 데이터 없음: {code}")
            else:
                logging.warning(f"⚠️ 차트 데이터 수집 실패: {code} - 틱데이터: {tick_data is not None}, 분봉데이터: {min_data is not None}")
                logging.info(f"💡 API 제한으로 인한 실패일 수 있습니다. 잠시 후 자동으로 재시도됩니다.")
            
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 업데이트 실패 ({code}): {ex}")
            import traceback
            logging.error(f"차트 업데이트 예외 상세: {traceback.format_exc()}")
    
    def update_all_charts(self):
        """모든 모니터링 종목 차트 데이터 업데이트 - 순차 처리"""
        try:
            cached_codes = list(self.cache.keys())
            logging.info(f"🔧 전체 차트 데이터 업데이트 시작 - 캐시된 종목: {cached_codes}")
            
            if not cached_codes:
                logging.warning("⚠️ 캐시된 종목이 없습니다")
                return
            
            # 종목들을 순차적으로 처리 (API 제한 방지)
            for i, code in enumerate(cached_codes):
                logging.info(f"🔧 차트 데이터 업데이트 시작: {code} ({i+1}/{len(cached_codes)})")
                self.update_single_chart(code)
                
                # 마지막 종목이 아니면 잠시 대기 (API 제한 방지)
                if i < len(cached_codes) - 1:
                    # QTimer를 사용한 비동기 대기 (UI 블로킹 방지)
                    QTimer.singleShot(1000, lambda: None)  # 1초 대기
                    logging.info(f"⏳ API 제한 방지를 위해 1초 대기 중...")
            
            logging.info(f"✅ 전체 차트 데이터 업데이트 완료 - 처리된 종목: {len(cached_codes)}개")
            
        except Exception as ex:
            logging.error(f"❌ 전체 차트 데이터 업데이트 실패: {ex}")
            import traceback
            logging.error(f"전체 업데이트 예외 상세: {traceback.format_exc()}")
    
    def get_chart_data(self, code):
        """캐시된 차트 데이터 조회"""
        try:
            cached_data = self.cache.get(code, None)
            if cached_data:
                tick_data = cached_data.get('tick_data')
                min_data = cached_data.get('min_data')
                if tick_data and min_data:
                    tick_count = len(tick_data.get('close', []))
                    min_count = len(min_data.get('close', []))
                    logging.debug(f"📊 ChartDataCache에서 {code} 데이터 조회 성공 - 틱:{tick_count}개, 분봉:{min_count}개")
                    return cached_data
                else:
                    logging.debug(f"📊 ChartDataCache에 {code} 데이터가 있지만 틱/분봉 데이터가 없음")
                    return None
            else:
                logging.debug(f"📊 ChartDataCache에 {code} 데이터가 없음")
                return None
        except Exception as ex:
            logging.error(f"ChartDataCache 데이터 조회 실패 ({code}): {ex}")
            return None
    
    def save_chart_data(self, code, tick_data, min_data):
        """차트 데이터를 캐시에 저장"""
        try:
            from datetime import datetime
            
            self.cache[code] = {
                'tick_data': tick_data,
                'min_data': min_data,
                'last_update': datetime.now(),
                'last_save': None
            }
            
            tick_count = len(tick_data.get('close', [])) if tick_data else 0
            min_count = len(min_data.get('close', [])) if min_data else 0
            
            logging.info(f"📊 ChartDataCache에 {code} 데이터 저장 완료 - 틱:{tick_count}개, 분봉:{min_count}개")
            return True
            
        except Exception as ex:
            logging.error(f"ChartDataCache 데이터 저장 실패 ({code}): {ex}")
            return False
    
    def get_tick_data_from_api(self, code, max_retries=3):
        """30틱봉 데이터 조회 (재시도 로직 포함)"""
        import time
        
        for attempt in range(max_retries):
            try:
                # API 요청 간격 조정 (첫 번째 시도가 아닌 경우 대기)
                if attempt > 0:
                    wait_time = 2 ** attempt  # 지수 백오프: 2초, 4초, 8초
                    logging.info(f"⏳ API 제한 대기 중... ({wait_time}초 후 재시도 {attempt + 1}/{max_retries})")
                    # QTimer를 사용한 비동기 대기 (UI 블로킹 방지)
                    QTimer.singleShot(int(wait_time * 1000), lambda: None)
                
                logging.info(f"🔧 API 틱 데이터 조회 시작: {code} (시도 {attempt + 1}/{max_retries})")
                data = self.trader.client.get_stock_tick_chart(code, tic_scope=30, count=300)
                
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
                    
                logging.info(f"✅ 틱 데이터 조회 성공: {code} - 데이터 개수: {len(close_data)}")
                return data
                
            except Exception as ex:
                error_msg = str(ex)
                if "429" in error_msg or "허용된 요청 개수를 초과" in error_msg:
                    logging.warning(f"⚠️ API 제한으로 인한 틱 데이터 조회 실패 ({code}): {ex}")
                    if attempt < max_retries - 1:
                        logging.info(f"💡 재시도 예정 ({attempt + 1}/{max_retries})")
                        continue
                    else:
                        logging.error(f"❌ 최대 재시도 횟수 초과: {code}")
                        return None
                else:
                    logging.error(f"❌ 틱 데이터 조회 실패 ({code}): {ex}")
                    import traceback
                    logging.error(f"틱 데이터 조회 예외 상세: {traceback.format_exc()}")
                    return None
        
        return None
    
    def get_min_data_from_api(self, code, max_retries=3):
        """3분봉 데이터 조회 (재시도 로직 포함)"""
        import time
        
        for attempt in range(max_retries):
            try:
                # API 요청 간격 조정 (첫 번째 시도가 아닌 경우 대기)
                if attempt > 0:
                    wait_time = 2 ** attempt  # 지수 백오프: 2초, 4초, 8초
                    logging.info(f"⏳ API 제한 대기 중... ({wait_time}초 후 재시도 {attempt + 1}/{max_retries})")
                    # QTimer를 사용한 비동기 대기 (UI 블로킹 방지)
                    QTimer.singleShot(int(wait_time * 1000), lambda: None)
                
                logging.info(f"🔧 API 분봉 데이터 조회 시작: {code} (시도 {attempt + 1}/{max_retries})")
                data = self.trader.client.get_stock_minute_chart(code, period=3, count=150)
                
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
                    
                logging.info(f"✅ 분봉 데이터 조회 성공: {code} - 데이터 개수: {len(close_data)}")
                return data
                
            except Exception as ex:
                error_msg = str(ex)
                if "429" in error_msg or "허용된 요청 개수를 초과" in error_msg:
                    logging.warning(f"⚠️ API 제한으로 인한 분봉 데이터 조회 실패 ({code}): {ex}")
                    if attempt < max_retries - 1:
                        logging.info(f"💡 재시도 예정 ({attempt + 1}/{max_retries})")
                        continue
                    else:
                        logging.error(f"❌ 최대 재시도 횟수 초과: {code}")
                        return None
                else:
                    logging.error(f"❌ 분봉 데이터 조회 실패 ({code}): {ex}")
                    import traceback
                    logging.error(f"분봉 데이터 조회 예외 상세: {traceback.format_exc()}")
                    return None
        
        return None
    
    def _trigger_async_save_to_database(self):
        """비동기 데이터베이스 저장 트리거"""
        try:
            import asyncio
            import concurrent.futures
            
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
            if not hasattr(self.trader, 'db_manager') or not self.trader.db_manager:
                return
            
            current_time = datetime.now()
            saved_count = 0
            
            for code, data in self.cache.items():
                if not data.get('tick_data') or not data.get('min_data'):
                    continue
                
                # 60초마다 저장 (마지막 저장 시간 확인)
                last_save = data.get('last_save')
                if last_save and (current_time - last_save).seconds < 60:
                    continue
                
                # 틱 데이터 저장 (비동기)
                await self.save_tick_data_to_db(code, data['tick_data'])
                
                # 분봉 데이터 저장 (비동기)
                await self.save_minute_data_to_db(code, data['min_data'])
                
                # 저장 시간 업데이트
                data['last_save'] = current_time
                saved_count += 1
            
            if saved_count > 0:
                logging.info(f"📊 차트 데이터 DB 저장 완료: {saved_count}개 종목")
                
        except Exception as ex:
            logging.error(f"차트 데이터 DB 저장 실패: {ex}")
    
    async def save_tick_data_to_db(self, code, tick_data):
        """틱 데이터를 DB에 저장 (비동기 I/O)"""
        try:
            if hasattr(self.trader, 'db_manager') and self.trader.db_manager:
                await self.trader.db_manager.save_tick_data(code, tick_data)
        except Exception as ex:
            logging.error(f"틱 데이터 DB 저장 실패 ({code}): {ex}")
    
    async def save_minute_data_to_db(self, code, min_data):
        """분봉 데이터를 DB에 저장 (비동기 I/O)"""
        try:
            if hasattr(self.trader, 'db_manager') and self.trader.db_manager:
                await self.trader.db_manager.save_minute_data(code, min_data)
        except Exception as ex:
            logging.error(f"분봉 데이터 DB 저장 실패 ({code}): {ex}")
    
    def log_single_stock_analysis(self, code, tick_data, min_data):
        """단일 종목 분석표 출력 (차트 데이터 저장 시) - 비활성화됨"""
        try:
            # 종목명 조회
            stock_name = self.get_stock_name(code)
            
            # 분석표 출력 비활성화 - 간단한 로그만 출력
            logging.info(f"📊 {stock_name}({code}) 차트 데이터 저장 완료")
            
            # 분석표 출력 부분 주석 처리
            # logging.info("=" * 120)
            # logging.info(f"📊 {stock_name}({code}) 차트 데이터 저장 완료 - 분석표")
            # logging.info("=" * 120)
            
            # # 틱 데이터 분석표 출력
            # if tick_data and len(tick_data.get('close', [])) > 0:
            #     self.log_ohlc_indicators_table(tick_data, f"{stock_name}({code}) - 30틱봉", "tick")
            
            # # 분봉 데이터 분석표 출력
            # if min_data and len(min_data.get('close', [])) > 0:
            #     self.log_ohlc_indicators_table(min_data, f"{stock_name}({code}) - 3분봉", "minute")
            
            # logging.info("=" * 120)
            
        except Exception as ex:
            logging.error(f"단일 종목 분석표 출력 실패 ({code}): {ex}")
    
    def log_all_monitoring_analysis(self):
        """모든 모니터링 종목에 대한 분석표 출력 - 비활성화됨"""
        try:
            if not self.cache:
                return
            
            # 분석표 출력 비활성화 - 간단한 로그만 출력
            logging.info(f"📊 모든 모니터링 종목 분석표 완료 - 캐시된 종목: {len(self.cache)}개")
            
            # 분석표 출력 부분 주석 처리
            # logging.info("=" * 150)
            # logging.info("📊 모든 모니터링 종목 분석표")
            # logging.info("=" * 150)
            
            # for code, data in self.cache.items():
            #     if not data.get('tick_data') or not data.get('min_data'):
            #         continue
                
            #     # 종목명 조회
            #     stock_name = self.get_stock_name(code)
                
            #     # 틱 데이터 분석표 출력
            #     if data['tick_data'] and len(data['tick_data'].get('close', [])) > 0:
            #         self.log_ohlc_indicators_table(data['tick_data'], f"{stock_name}({code}) - 30틱봉", "tick")
                
            #     # 분봉 데이터 분석표 출력
            #     if data['min_data'] and len(data['min_data'].get('close', [])) > 0:
            #         self.log_ohlc_indicators_table(data['min_data'], f"{stock_name}({code}) - 3분봉", "minute")
                
            #     logging.info("-" * 150)
            
            # logging.info("📊 모든 모니터링 종목 분석표 완료")
            
        except Exception as ex:
            logging.error(f"모니터링 종목 분석표 출력 실패: {ex}")
    
    def get_stock_name(self, code):
        """종목코드로 종목명 조회"""
        try:
            # 간단한 종목명 매핑 (실제로는 API에서 조회해야 함)
            stock_names = {
                '005930': '삼성전자',
                '005380': '현대차',
                '000660': 'SK하이닉스',
                '035420': 'NAVER',
                '051910': 'LG화학',
                '006400': '삼성SDI',
                '035720': '카카오',
                '207940': '삼성바이오로직스',
                '068270': '셀트리온',
                '323410': '카카오뱅크'
            }
            return stock_names.get(code, f"종목{code}")
        except Exception:
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
            # TechnicalIndicatorsThread의 메서드를 직접 사용
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
            logging.info("=" * 120)
            logging.info(f"📊 {title} OHLC & 기술적지표 분석표")
            logging.info("=" * 120)
            logging.info(f"{'시간':<8} {'시가':<8} {'고가':<8} {'저가':<8} {'종가':<8} {'SMA5':<8} {'SMA20':<8} {'RSI':<6} {'MACD':<8} {'Signal':<8} {'Hist':<8}")
            logging.info("-" * 120)
            
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
                logging.info(f"{time_str:<8} {opens[i]:<8.0f} {highs[i]:<8.0f} {lows[i]:<8.0f} {closes[i]:<8.0f} {sma5_val:<8} {sma20_val:<8} {rsi_val:<6} {macd_val:<8} {signal_val:<8} {hist_val:<8}")
            
            logging.info("-" * 120)
            
        except Exception as ex:
            logging.error(f"OHLC 분석표 출력 실패: {ex}")
    
    def update_realtime_data(self, code, execution_data):
        """실시간 체결 데이터로 차트 업데이트"""
        try:
            if code not in self.cache:
                logging.debug(f"⚠️ 캐시에 없는 종목: {code}")
                return
            
            # 체결 데이터 파싱
            current_price = execution_data.get('execution_price', 0)
            execution_volume = execution_data.get('execution_volume', 0)
            execution_time = execution_data.get('execution_time', '')
            
            if current_price <= 0 or execution_volume <= 0:
                return
            
            # 시간 파싱 (HHMMSS 형식)
            try:
                if len(execution_time) >= 6:
                    time_val = int(execution_time[:6])  # HHMMSS
                else:
                    return
            except (ValueError, TypeError):
                return
            
            current_time = datetime.now()
            cached_data = self.cache[code]
            
            # 30틱봉 업데이트
            self._update_tick_chart(code, time_val, current_price, execution_volume, current_time)
            
            # 3분봉 업데이트
            self._update_minute_chart(code, time_val, current_price, execution_volume, current_time)
            
            # 캐시 데이터 업데이트 시간 갱신
            cached_data['last_update'] = current_time
            
            # 실시간 기술적 지표 즉시 계산
            self._calculate_technical_indicators(code)
            
            logging.debug(f"📊 실시간 차트 업데이트: {code} - 가격: {current_price:,}, 수량: {execution_volume:,}")
            
        except Exception as ex:
            logging.error(f"❌ 실시간 차트 업데이트 실패 ({code}): {ex}")
            import traceback
            logging.error(f"실시간 업데이트 예외 상세: {traceback.format_exc()}")
    
    def _update_tick_chart(self, code, time_val, price, volume, current_time):
        """30틱봉 실시간 업데이트"""
        try:
            cached_data = self.cache[code]
            tick_data = cached_data.get('tick_data')
            
            if not tick_data:
                return
            
            # 시간 변환 (HHMMSS -> HHMM)
            hh, mm = divmod(time_val, 10000)
            mm, ss = divmod(mm, 100)
            if mm == 60:
                hh += 1
                mm = 0
            lCurTime = hh * 100 + mm
            
            # 현재 시간이 마지막 시간보다 크면 제한
            if lCurTime > 1530:  # 15:30 제한
                lCurTime = 1530
            
            times = tick_data.get('time', [])
            opens = tick_data.get('open', [])
            highs = tick_data.get('high', [])
            lows = tick_data.get('low', [])
            closes = tick_data.get('close', [])
            volumes = tick_data.get('volume', [])
            last_tic_cnts = tick_data.get('last_tic_cnt', [])
            
            if not times:
                return
            
            # 마지막 틱 데이터 확인
            last_tic_cnt = last_tic_cnts[-1] if last_tic_cnts else 0
            try:
                last_tic_cnt = int(last_tic_cnt) if last_tic_cnt else 0
            except (ValueError, TypeError):
                last_tic_cnt = 0
            
            bFind = False
            
            # 현재 틱이 30틱 미만이면 기존 봉 업데이트
            if 1 <= last_tic_cnt < 30:
                bFind = True
                times[-1] = lCurTime
                closes[-1] = price
                if highs[-1] < price:
                    highs[-1] = price
                if lows[-1] > price:
                    lows[-1] = price
                volumes[-1] += volume
                last_tic_cnts[-1] = str(last_tic_cnt + 1)
            
            # 새 봉 생성
            if not bFind:
                times.append(lCurTime)
                opens.append(price)
                highs.append(price)
                lows.append(price)
                closes.append(price)
                volumes.append(volume)
                last_tic_cnts.append('1')
                
                # 최대 300개 데이터 유지
                max_length = 300
                for key in ['time', 'open', 'high', 'low', 'close', 'volume', 'last_tic_cnt']:
                    if key in tick_data and len(tick_data[key]) > max_length:
                        tick_data[key] = tick_data[key][-max_length:]
            
            logging.debug(f"📊 30틱봉 업데이트: {code} - 시간: {lCurTime}, 가격: {price:,}, 틱수: {last_tic_cnts[-1]}")
            
        except Exception as ex:
            logging.error(f"❌ 30틱봉 업데이트 실패 ({code}): {ex}")
    
    def _update_minute_chart(self, code, time_val, price, volume, current_time):
        """3분봉 실시간 업데이트"""
        try:
            cached_data = self.cache[code]
            min_data = cached_data.get('min_data')
            
            if not min_data:
                return
            
            # 시간 변환 (HHMMSS -> 분 단위)
            hh, mm = divmod(time_val, 10000)
            mm, ss = divmod(mm, 100)
            converted_min_time = hh * 60 + mm
            
            # 3분 간격으로 변환
            interval = 3
            a, b = divmod(converted_min_time, interval)
            interval_time = a * interval
            l_chart_time = interval_time + interval
            hour, minute = divmod(l_chart_time, 60)
            lCurTime = hour * 100 + minute
            
            # 현재 시간이 마지막 시간보다 크면 제한
            if lCurTime > 1530:  # 15:30 제한
                lCurTime = 1530
            
            times = min_data.get('time', [])
            opens = min_data.get('open', [])
            highs = min_data.get('high', [])
            lows = min_data.get('low', [])
            closes = min_data.get('close', [])
            volumes = min_data.get('volume', [])
            
            if not times:
                return
            
            bFind = False
            
            # 같은 시간대면 기존 봉 업데이트
            if times and times[-1] == lCurTime:
                bFind = True
                closes[-1] = price
                if highs[-1] < price:
                    highs[-1] = price
                if lows[-1] > price:
                    lows[-1] = price
                volumes[-1] += volume
            
            # 새 봉 생성
            if not bFind:
                times.append(lCurTime)
                opens.append(price)
                highs.append(price)
                lows.append(price)
                closes.append(price)
                volumes.append(volume)
                
                # 최대 150개 데이터 유지
                max_length = 150
                for key in ['time', 'open', 'high', 'low', 'close', 'volume']:
                    if key in min_data and len(min_data[key]) > max_length:
                        min_data[key] = min_data[key][-max_length:]
            
            logging.debug(f"📊 3분봉 업데이트: {code} - 시간: {lCurTime}, 가격: {price:,}")
            
        except Exception as ex:
            logging.error(f"❌ 3분봉 업데이트 실패 ({code}): {ex}")
    
    def _calculate_technical_indicators(self, code):
        """실시간 기술적 지표 계산 (ta-lib 사용)"""
        try:
            if code not in self.cache:
                return
            
            cached_data = self.cache[code]
            tick_data = cached_data.get('tick_data')
            min_data = cached_data.get('min_data')
            
            if not tick_data or not min_data:
                return
            
            # 30틱봉 기술적 지표 계산
            self._calculate_tick_indicators(code, tick_data)
            
            # 3분봉 기술적 지표 계산
            self._calculate_minute_indicators(code, min_data)
            
            logging.debug(f"📊 기술적 지표 계산 완료: {code}")
            
        except Exception as ex:
            logging.error(f"❌ 기술적 지표 계산 실패 ({code}): {ex}")
    
    def _calculate_tick_indicators(self, code, tick_data):
        """30틱봉 기술적 지표 계산 (ta-lib 사용)"""
        try:
            closes = tick_data.get('close', [])
            highs = tick_data.get('high', [])
            lows = tick_data.get('low', [])
            volumes = tick_data.get('volume', [])
            
            if len(closes) < 20:  # 최소 데이터 요구량
                return
            
            # numpy 배열로 변환
            import numpy as np
            close_array = np.array(closes, dtype=float)
            high_array = np.array(highs, dtype=float)
            low_array = np.array(lows, dtype=float)
            volume_array = np.array(volumes, dtype=float)
            
            # 기술적 지표 계산 (ta-lib 사용)
            indicators = {}
            
            # 이동평균선 (MA)
            if len(closes) >= 5:
                indicators['MA5'] = talib.SMA(close_array, timeperiod=5)[-1]
            if len(closes) >= 10:
                indicators['MA10'] = talib.SMA(close_array, timeperiod=10)[-1]
            if len(closes) >= 20:
                indicators['MA20'] = talib.SMA(close_array, timeperiod=20)[-1]
            if len(closes) >= 50:
                indicators['MA50'] = talib.SMA(close_array, timeperiod=50)[-1]
            
            # RSI 계산
            if len(closes) >= 14:
                rsi = talib.RSI(close_array, timeperiod=14)
                indicators['RSI'] = rsi[-1] if not np.isnan(rsi[-1]) else 50
            
            # MACD 계산
            if len(closes) >= 26:
                macd, macd_signal, macd_hist = talib.MACD(close_array)
                indicators['MACD'] = macd[-1] if not np.isnan(macd[-1]) else 0
                indicators['MACD_SIGNAL'] = macd_signal[-1] if not np.isnan(macd_signal[-1]) else 0
                indicators['MACD_HIST'] = macd_hist[-1] if not np.isnan(macd_hist[-1]) else 0
            
            # 볼린저 밴드
            if len(closes) >= 20:
                bb_upper, bb_middle, bb_lower = talib.BBANDS(close_array, timeperiod=20)
                indicators['BB_UPPER'] = bb_upper[-1] if not np.isnan(bb_upper[-1]) else 0
                indicators['BB_MIDDLE'] = bb_middle[-1] if not np.isnan(bb_middle[-1]) else 0
                indicators['BB_LOWER'] = bb_lower[-1] if not np.isnan(bb_lower[-1]) else 0
            
            # 스토캐스틱
            if len(closes) >= 14:
                slowk, slowd = talib.STOCH(high_array, low_array, close_array)
                indicators['STOCH_K'] = slowk[-1] if not np.isnan(slowk[-1]) else 50
                indicators['STOCH_D'] = slowd[-1] if not np.isnan(slowd[-1]) else 50
            
            # ATR (Average True Range)
            if len(closes) >= 14:
                atr = talib.ATR(high_array, low_array, close_array, timeperiod=14)
                indicators['ATR'] = atr[-1] if not np.isnan(atr[-1]) else 0
            
            # CCI (Commodity Channel Index)
            if len(closes) >= 14:
                cci = talib.CCI(high_array, low_array, close_array, timeperiod=14)
                indicators['CCI'] = cci[-1] if not np.isnan(cci[-1]) else 0
            
            # Williams %R
            if len(closes) >= 14:
                willr = talib.WILLR(high_array, low_array, close_array, timeperiod=14)
                indicators['WILLR'] = willr[-1] if not np.isnan(willr[-1]) else -50
            
            # ROC (Rate of Change)
            if len(closes) >= 10:
                roc = talib.ROC(close_array, timeperiod=10)
                indicators['ROC'] = roc[-1] if not np.isnan(roc[-1]) else 0
            
            # OBV (On Balance Volume)
            if len(closes) >= 2:
                obv = talib.OBV(close_array, volume_array)
                indicators['OBV'] = obv[-1] if not np.isnan(obv[-1]) else 0
            
            # OSC (Oscillator) - MACD 히스토그램
            if len(closes) >= 26:
                macd, macd_signal, macd_hist = talib.MACD(close_array)
                indicators['OSC'] = macd_hist[-1] if not np.isnan(macd_hist[-1]) else 0
                indicators['OSCT'] = macd_hist[-1] if not np.isnan(macd_hist[-1]) else 0
            
            # 볼린저 밴드 대역폭
            if len(closes) >= 20:
                bb_upper, bb_middle, bb_lower = talib.BBANDS(close_array, timeperiod=20)
                if not np.isnan(bb_upper[-1]) and not np.isnan(bb_lower[-1]) and bb_middle[-1] > 0:
                    indicators['BB_BANDWIDTH'] = (bb_upper[-1] - bb_lower[-1]) / bb_middle[-1]
                else:
                    indicators['BB_BANDWIDTH'] = 0
            
            # RSI 신호선 (RSI의 이동평균)
            if len(closes) >= 14:
                rsi = talib.RSI(close_array, timeperiod=14)
                if len(rsi) >= 5:
                    rsi_signal = talib.SMA(rsi, timeperiod=5)
                    indicators['RSIT_SIGNAL'] = rsi_signal[-1] if not np.isnan(rsi_signal[-1]) else 50
                else:
                    indicators['RSIT_SIGNAL'] = 50
            
            # 틱봉 가격 정보 (전략에서 사용)
            indicators['tick_close_price'] = closes[-1] if closes else 0
            indicators['tick_high_price'] = highs[-1] if highs else 0
            indicators['tick_low_price'] = lows[-1] if lows else 0
            
            # 캐시에 지표 저장
            if 'indicators' not in self.cache[code]:
                self.cache[code]['indicators'] = {}
            
            self.cache[code]['indicators']['tick'] = indicators
            
        except Exception as ex:
            logging.error(f"❌ 30틱봉 지표 계산 실패 ({code}): {ex}")
    
    def _calculate_minute_indicators(self, code, min_data):
        """3분봉 기술적 지표 계산 (ta-lib 사용)"""
        try:
            closes = min_data.get('close', [])
            highs = min_data.get('high', [])
            lows = min_data.get('low', [])
            volumes = min_data.get('volume', [])
            
            if len(closes) < 20:  # 최소 데이터 요구량
                return
            
            # numpy 배열로 변환
            import numpy as np
            close_array = np.array(closes, dtype=float)
            high_array = np.array(highs, dtype=float)
            low_array = np.array(lows, dtype=float)
            volume_array = np.array(volumes, dtype=float)
            
            # 기술적 지표 계산 (ta-lib 사용)
            indicators = {}
            
            # 이동평균선 (MA)
            if len(closes) >= 5:
                indicators['MA5'] = talib.SMA(close_array, timeperiod=5)[-1]
            if len(closes) >= 10:
                indicators['MA10'] = talib.SMA(close_array, timeperiod=10)[-1]
            if len(closes) >= 20:
                indicators['MA20'] = talib.SMA(close_array, timeperiod=20)[-1]
            if len(closes) >= 50:
                indicators['MA50'] = talib.SMA(close_array, timeperiod=50)[-1]
            
            # RSI 계산
            if len(closes) >= 14:
                rsi = talib.RSI(close_array, timeperiod=14)
                indicators['RSI'] = rsi[-1] if not np.isnan(rsi[-1]) else 50
            
            # MACD 계산
            if len(closes) >= 26:
                macd, macd_signal, macd_hist = talib.MACD(close_array)
                indicators['MACD'] = macd[-1] if not np.isnan(macd[-1]) else 0
                indicators['MACD_SIGNAL'] = macd_signal[-1] if not np.isnan(macd_signal[-1]) else 0
                indicators['MACD_HIST'] = macd_hist[-1] if not np.isnan(macd_hist[-1]) else 0
            
            # 볼린저 밴드
            if len(closes) >= 20:
                bb_upper, bb_middle, bb_lower = talib.BBANDS(close_array, timeperiod=20)
                indicators['BB_UPPER'] = bb_upper[-1] if not np.isnan(bb_upper[-1]) else 0
                indicators['BB_MIDDLE'] = bb_middle[-1] if not np.isnan(bb_middle[-1]) else 0
                indicators['BB_LOWER'] = bb_lower[-1] if not np.isnan(bb_lower[-1]) else 0
            
            # 스토캐스틱
            if len(closes) >= 14:
                slowk, slowd = talib.STOCH(high_array, low_array, close_array)
                indicators['STOCH_K'] = slowk[-1] if not np.isnan(slowk[-1]) else 50
                indicators['STOCH_D'] = slowd[-1] if not np.isnan(slowd[-1]) else 50
            
            # ATR (Average True Range)
            if len(closes) >= 14:
                atr = talib.ATR(high_array, low_array, close_array, timeperiod=14)
                indicators['ATR'] = atr[-1] if not np.isnan(atr[-1]) else 0
            
            # CCI (Commodity Channel Index)
            if len(closes) >= 14:
                cci = talib.CCI(high_array, low_array, close_array, timeperiod=14)
                indicators['CCI'] = cci[-1] if not np.isnan(cci[-1]) else 0
            
            # Williams %R
            if len(closes) >= 14:
                willr = talib.WILLR(high_array, low_array, close_array, timeperiod=14)
                indicators['WILLR'] = willr[-1] if not np.isnan(willr[-1]) else -50
            
            # ROC (Rate of Change)
            if len(closes) >= 10:
                roc = talib.ROC(close_array, timeperiod=10)
                indicators['ROC'] = roc[-1] if not np.isnan(roc[-1]) else 0
            
            # OBV (On Balance Volume)
            if len(closes) >= 2:
                obv = talib.OBV(close_array, volume_array)
                indicators['OBV'] = obv[-1] if not np.isnan(obv[-1]) else 0
                
                # OBV 이동평균
                if len(obv) >= 20:
                    obv_ma20 = talib.SMA(obv, timeperiod=20)
                    indicators['OBV_MA20'] = obv_ma20[-1] if not np.isnan(obv_ma20[-1]) else 0
            
            # OSC (Oscillator) - MACD 히스토그램
            if len(closes) >= 26:
                macd, macd_signal, macd_hist = talib.MACD(close_array)
                indicators['OSC'] = macd_hist[-1] if not np.isnan(macd_hist[-1]) else 0
                indicators['OSCT'] = macd_hist[-1] if not np.isnan(macd_hist[-1]) else 0
            
            # 볼린저 밴드 대역폭
            if len(closes) >= 20:
                bb_upper, bb_middle, bb_lower = talib.BBANDS(close_array, timeperiod=20)
                if not np.isnan(bb_upper[-1]) and not np.isnan(bb_lower[-1]) and bb_middle[-1] > 0:
                    indicators['BB_BANDWIDTH'] = (bb_upper[-1] - bb_lower[-1]) / bb_middle[-1]
                else:
                    indicators['BB_BANDWIDTH'] = 0
            
            # RSI 신호선 (RSI의 이동평균)
            if len(closes) >= 14:
                rsi = talib.RSI(close_array, timeperiod=14)
                if len(rsi) >= 5:
                    rsi_signal = talib.SMA(rsi, timeperiod=5)
                    indicators['RSIT_SIGNAL'] = rsi_signal[-1] if not np.isnan(rsi_signal[-1]) else 50
                else:
                    indicators['RSIT_SIGNAL'] = 50
            
            # 분봉 가격 정보 (전략에서 사용)
            indicators['min_close_price'] = closes[-1] if closes else 0
            indicators['min_high_price'] = highs[-1] if highs else 0
            indicators['min_low_price'] = lows[-1] if lows else 0
            
            # 분봉 이동평균 (전략에서 사용)
            if len(closes) >= 5:
                indicators['MAM5'] = talib.SMA(close_array, timeperiod=5)[-1]
            if len(closes) >= 10:
                indicators['MAM10'] = talib.SMA(close_array, timeperiod=10)[-1]
            if len(closes) >= 20:
                indicators['MAM20'] = talib.SMA(close_array, timeperiod=20)[-1]
            
            # 분봉 최소값들 (전략에서 사용)
            if len(closes) >= 30:
                indicators['min_close'] = min(closes[-30:])
            if 'RSI' in indicators and len(indicators['RSI']) >= 30:
                indicators['min_RSI'] = min(indicators['RSI'][-30:])
            if 'STOCHK' in indicators:
                indicators['min_STOCHK'] = indicators['STOCHK']  # 단일 값이므로 현재값 사용
            if 'STOCHD' in indicators:
                indicators['min_STOCHD'] = indicators['STOCHD']  # 단일 값이므로 현재값 사용
            if 'WILLIAMS_R' in indicators:
                indicators['min_WILLIAMS_R'] = indicators['WILLIAMS_R']  # 단일 값이므로 현재값 사용
            if 'OSC' in indicators:
                indicators['min_OSC'] = indicators['OSC']  # 단일 값이므로 현재값 사용
            
            # 캐시에 지표 저장
            if 'indicators' not in self.cache[code]:
                self.cache[code]['indicators'] = {}
            
            self.cache[code]['indicators']['minute'] = indicators
            
        except Exception as ex:
            logging.error(f"❌ 3분봉 지표 계산 실패 ({code}): {ex}")
    
    def get_technical_indicators(self, code):
        """기술적 지표 조회"""
        try:
            if code not in self.cache:
                return None
            
            cached_data = self.cache[code]
            indicators = cached_data.get('indicators', {})
            
            return {
                'tick_indicators': indicators.get('tick', {}),
                'minute_indicators': indicators.get('minute', {}),
                'last_update': cached_data.get('last_update')
            }
            
        except Exception as ex:
            logging.error(f"❌ 기술적 지표 조회 실패 ({code}): {ex}")
            return None

    def stop(self):
        """캐시 정리"""
        try:
            if self.update_timer:
                self.update_timer.stop()
            if self.save_timer:
                self.save_timer.stop()
            self.cache.clear()
            logging.info("📊 차트 데이터 캐시 정리 완료")
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 캐시 정리 실패: {ex}")
            import traceback
            logging.error(f"캐시 정리 예외 상세: {traceback.format_exc()}")


# ==================== API 요청 스레드 클래스 ====================
class ApiRequestThread(QThread):
    """API 요청을 위한 별도 스레드 (UI 블로킹 방지)"""
    data_ready = pyqtSignal(dict)  # 데이터 준비 완료 시그널
    error_occurred = pyqtSignal(str)  # 에러 발생 시그널
    progress_updated = pyqtSignal(str)  # 진행 상황 업데이트 시그널
    
    def __init__(self, client, code, request_type, **kwargs):
        super().__init__()
        self.client = client
        self.code = code
        self.request_type = request_type
        self.kwargs = kwargs
        self._is_cancelled = False
        
    def cancel(self):
        """요청 취소"""
        self._is_cancelled = True
        
    def run(self):
        """스레드 실행"""
        try:
            if self._is_cancelled:
                return
                
            self.progress_updated.emit(f"API 요청 시작: {self.code} ({self.request_type})")
            
            # API 제한 확인 (호환성을 위해)
            if not ApiLimitManager.check_api_limit_and_wait(request_type=self.request_type):
                # 대기가 필요한 경우 잠시 대기
                import time
                time.sleep(0.1)
            
            if self.request_type == 'tick':
                data = self.client.get_stock_tick_chart(
                    self.code, 
                    tic_scope=self.kwargs.get('tic_scope', 30), 
                    count=self.kwargs.get('count', 300)
                )
            elif self.request_type == 'minute':
                data = self.client.get_stock_minute_chart(
                    self.code, 
                    count=self.kwargs.get('count', 100)
                )
            else:
                raise ValueError(f"지원하지 않는 요청 타입: {self.request_type}")
            
            if self._is_cancelled:
                return
                
            self.progress_updated.emit(f"API 요청 완료: {self.code} ({self.request_type})")
            self.data_ready.emit(data)
            
        except Exception as e:
            if not self._is_cancelled:
                self.error_occurred.emit(f"API 요청 실패 ({self.request_type}): {str(e)}")


class ApiRequestManager(QObject):
    """API 제한 관리를 위한 QTimer 기반 대기 시스템"""
    request_ready = pyqtSignal(object, str, dict)  # 요청 준비 시그널
    
    def __init__(self):
        super().__init__()
        self.timer = QTimer()
        self.timer.timeout.connect(self._on_timeout)
        self.pending_requests = []
        self._last_request_time = {}
        
    def request_with_delay(self, client, code, request_type, delay_seconds=0, **kwargs):
        """지연된 API 요청"""
        request_info = {
            'client': client,
            'code': code,
            'request_type': request_type,
            'kwargs': kwargs,
            'timestamp': time.time()
        }
        
        if delay_seconds > 0:
            self.pending_requests.append(request_info)
            self.timer.setSingleShot(True)
            self.timer.start(int(delay_seconds * 1000))  # 밀리초로 변환
        else:
            self._execute_request(request_info)
            
    def _on_timeout(self):
        """타이머 완료 시 대기 중인 요청 실행"""
        if self.pending_requests:
            request_info = self.pending_requests.pop(0)
            self._execute_request(request_info)
            
    def _execute_request(self, request_info):
        """실제 요청 실행"""
        # API 요청 간격 조정
        request_type = request_info['request_type']
        current_time = time.time()
        
        if request_type in self._last_request_time:
            elapsed = current_time - self._last_request_time[request_type]
            min_interval = 0.5 if request_type == 'tick' else 0.2  # 최소 간격
            
            if elapsed < min_interval:
                # 추가 대기 필요 (QTimer 사용)
                additional_delay = min_interval - elapsed
                logging.debug(f"⏳ API 요청 간격 조정: {additional_delay:.2f}초 대기 ({request_type})")
                self.request_with_delay(
                    request_info['client'],
                    request_info['code'],
                    request_info['request_type'],
                    additional_delay,
                    **request_info['kwargs']
                )
                return
        
        # 요청 실행
        self._last_request_time[request_type] = current_time
        self.request_ready.emit(
            request_info['client'],
            request_info['request_type'],
            {
                'code': request_info['code'],
                'kwargs': request_info['kwargs']
            }
        )
    
    def check_api_limit_and_wait(self, request_type):
        """API 제한 확인 및 대기 (호환성을 위한 메서드)"""
        # 클래스 메서드를 호출하여 일관성 유지
        return ApiLimitManager.check_api_limit_and_wait(request_type=request_type)


# ==================== 차트 관련 클래스 ====================
class ChartDataProcessor(QThread):
    """차트 데이터 처리 스레드 (CPU 바운드 작업)"""
    data_ready = pyqtSignal(dict)

    def __init__(self, trader, code):
        super().__init__()
        self.trader = trader
        self.code = code
        self.is_running = True
        
        # QThread에서 emit할 때 QTextCursor 메타타입 재등록
        register_qtextcursor_metatype()

    def run(self):
        while self.is_running:
            if self.code:
                try:
                    # 키움 REST API 연결 상태 확인
                    if not self.trader:
                        error_msg = f"트레이더 객체가 없습니다. 종목: {self.code}"
                        logging.error(error_msg)
                        self.data_ready.emit({
                            'code': self.code,
                            'error': error_msg,
                            'tick_data': None,
                            'min_data': None
                        })
                        self.msleep(5000)  # 에러 시 5초 대기
                        continue
                    
                    if not hasattr(self.trader, 'client') or not self.trader.client:
                        error_msg = f"API 클라이언트가 초기화되지 않았습니다. 종목: {self.code}"
                        logging.error(error_msg)
                        self.data_ready.emit({
                            'code': self.code,
                            'error': error_msg,
                            'tick_data': None,
                            'min_data': None
                        })
                        self.msleep(5000)  # 에러 시 5초 대기
                        continue
                    
                    if not self.trader.client.is_connected:
                        error_msg = f"API가 연결되지 않았습니다. 종목: {self.code}"
                        logging.error(error_msg)
                        self.data_ready.emit({
                            'code': self.code,
                            'error': error_msg,
                            'tick_data': None,
                            'min_data': None
                        })
                        self.msleep(5000)  # 에러 시 5초 대기
                        continue
                    
                    # 키움 REST API에서 실제 데이터 수집 (비동기 I/O)
                    tick_data = self._get_tick_data_sync(self.code)
                    min_data = self._get_min_data_sync(self.code)
                    
                    # 데이터 유효성 체크
                    tick_valid = (tick_data and 
                                 len(tick_data.get('close', [])) > 0)
                    
                    min_valid = (min_data and 
                                len(min_data.get('close', [])) > 0)
                    
                    # 둘 다 유효한 경우에만 emit
                    if tick_valid and min_valid:
                        data = {'tick_data': tick_data, 'min_data': min_data, 'code': self.code, 'error': None}
                        self.data_ready.emit(data)
                        logging.debug(f"📊 {self.code}: 차트 데이터 업데이트 완료")
                    else:
                        error_msg = f"데이터를 가져올 수 없습니다. 종목: {self.code}"
                        logging.error(error_msg)
                        self.data_ready.emit({
                            'code': self.code,
                            'error': error_msg,
                            'tick_data': None,
                            'min_data': None
                        })
                    
                except Exception as ex:
                    error_msg = f"차트 데이터 수집 중 오류: {ex}"
                    logging.error(error_msg)
                    self.data_ready.emit({
                        'code': self.code,
                        'error': error_msg,
                        'tick_data': None,
                        'min_data': None
                    })
                
            self.msleep(3000)  # 3초마다 업데이트 (API 호출 빈도 조절)

    def _get_tick_data_sync(self, code):
        """틱 데이터를 동기적으로 조회 (내부적으로 asyncio 사용)"""
        try:
            import asyncio
            import concurrent.futures
            
            def run_async_tick():
                try:
                    # 새로운 이벤트 루프 생성
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        # 비동기 틱 데이터 조회 실행
                        return loop.run_until_complete(self._get_tick_data_async(code))
                    finally:
                        loop.close()
                except Exception as e:
                    logging.error(f"비동기 틱 데이터 조회 실행 오류: {e}")
                    return None
            
            # 별도 스레드에서 비동기 조회 실행
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_async_tick)
                return future.result(timeout=10)  # 10초 타임아웃
                
        except Exception as ex:
            logging.error(f"틱 데이터 동기 조회 실패: {ex}")
            return None
    
    async def _get_tick_data_async(self, code):
        """틱 데이터 비동기 조회 (I/O 바운드 작업)"""
        try:
            if not self.trader or not hasattr(self.trader, 'client') or not self.trader.client:
                raise Exception("API 클라이언트가 연결되지 않았습니다")
            
            # 키움 REST API ka10079 (주식틱차트조회요청) 사용 - 300개 데이터 수집
            data = self.trader.client.get_stock_tick_chart(code, tic_scope=30, count=300)
            
            if not data or len(data.get('close', [])) == 0:
                raise Exception(f"틱 데이터가 비어있음: {code}")
            
            logging.debug(f"API에서 30틱 데이터 수집 완료: {code}, 데이터 수: {len(data['close'])}")
            return data
            
        except Exception as ex:
            logging.error(f"API 틱 데이터 조회 실패: {ex}")
            raise ex
    

    def _get_min_data_sync(self, code):
        """분봉 데이터를 동기적으로 조회 (내부적으로 asyncio 사용)"""
        try:
            import asyncio
            import concurrent.futures
            
            def run_async_min():
                try:
                    # 새로운 이벤트 루프 생성
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        # 비동기 분봉 데이터 조회 실행
                        return loop.run_until_complete(self._get_min_data_async(code))
                    finally:
                        loop.close()
                except Exception as e:
                    logging.error(f"비동기 분봉 데이터 조회 실행 오류: {e}")
                    return None
            
            # 별도 스레드에서 비동기 조회 실행
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_async_min)
                return future.result(timeout=10)  # 10초 타임아웃
                
        except Exception as ex:
            logging.error(f"분봉 데이터 동기 조회 실패: {ex}")
            return None
    
    async def _get_min_data_async(self, code):
        """분봉 데이터 비동기 조회 (I/O 바운드 작업)"""
        try:
            if not self.trader or not hasattr(self.trader, 'client') or not self.trader.client:
                raise Exception("API 클라이언트가 연결되지 않았습니다")
            
            # 키움 REST API에서 3분봉 데이터 조회 (분봉 차트 API 사용) - 150개 데이터 수집
            data = self.trader.client.get_stock_minute_chart(code, period=3, count=150)
            
            if not data or len(data.get('close', [])) == 0:
                raise Exception(f"3분봉 데이터가 비어있음: {code}")
            
            logging.debug(f"API에서 3분봉 데이터 수집 완료: {code}, 데이터 수: {len(data['close'])}")
            return data
            
        except Exception as ex:
            logging.error(f"API 3분봉 데이터 조회 실패: {ex}")
            raise ex
    

    def stop(self):
        """스레드 안전한 정리 (사용하지 않음 - 직접 호출 대신 quit/wait 사용)"""
        self.is_running = False
        # quit()과 wait()는 호출하는 쪽에서 직접 처리
        logging.info(f"ChartDataProcessor 스레드 정리 요청: {self.code}")


class TechnicalIndicatorsThread(QThread):
    """기술적지표 계산을 위한 QThread 클래스 (CPU 바운드 작업)"""
    
    # 시그널 정의
    indicators_calculated = pyqtSignal(dict)  # 계산 완료 시그널
    
    def __init__(self, prices, indicators_config):
        super().__init__()
        self.prices = prices
        self.indicators_config = indicators_config
        
    def run(self):
        """CPU 바운드 기술적 지표 계산 실행"""
        try:
            logging.debug("🔧 기술적 지표 계산 시작 (CPU 바운드 작업)")
            
            results = {}
            
            # SMA 계산
            if 'sma' in self.indicators_config:
                for period in self.indicators_config['sma']:
                    sma_result = self._calculate_sma(self.prices, period)
                    results[f'sma_{period}'] = sma_result
            
            # EMA 계산
            if 'ema' in self.indicators_config:
                for period in self.indicators_config['ema']:
                    ema_result = self._calculate_ema(self.prices, period)
                    results[f'ema_{period}'] = ema_result
            
            # RSI 계산
            if 'rsi' in self.indicators_config:
                for period in self.indicators_config['rsi']:
                    rsi_result = self._calculate_rsi(self.prices, period)
                    results[f'rsi_{period}'] = rsi_result
            
            # MACD 계산
            if 'macd' in self.indicators_config:
                macd_config = self.indicators_config['macd']
                macd_result = self._calculate_macd(
                    self.prices,
                    macd_config.get('fast_period', 12),
                    macd_config.get('slow_period', 26),
                    macd_config.get('signal_period', 9)
                )
                results['macd'] = macd_result
            
            # 결과 전송
            self.indicators_calculated.emit(results)
            logging.debug("✅ 기술적 지표 계산 완료")
            
        except Exception as e:
            logging.error(f"❌ 기술적 지표 계산 실패: {e}")
            import traceback
            logging.error(f"기술적 지표 계산 에러 상세: {traceback.format_exc()}")
            self.indicators_calculated.emit({'error': str(e)})
    
    def _calculate_sma(self, prices, period):
        """단순이동평균(Simple Moving Average) 계산"""
        if len(prices) < period:
            return []
        
        try:
            # TA-Lib을 사용하여 SMA 계산
            prices_array = np.array(prices, dtype=float)
            sma = talib.SMA(prices_array, timeperiod=period)
            
            # 시점 순서 유지: NaN을 0으로 대체
            sma_filled = np.where(np.isnan(sma), 0, sma)
            return sma_filled.tolist()
        except Exception as e:
            logging.error(f"SMA 계산 실패: {e}")
            return []
    
    def _calculate_ema(self, prices, period):
        """지수이동평균(Exponential Moving Average) 계산"""
        if len(prices) < period:
            return []
        
        try:
            # TA-Lib을 사용하여 EMA 계산
            prices_array = np.array(prices, dtype=float)
            ema = talib.EMA(prices_array, timeperiod=period)
            
            # 시점 순서 유지: NaN을 0으로 대체
            ema_filled = np.where(np.isnan(ema), 0, ema)
            return ema_filled.tolist()
        except Exception as e:
            logging.error(f"EMA 계산 실패: {e}")
            return []
    
    def _calculate_rsi(self, prices, period=14):
        """RSI(Relative Strength Index) 계산"""
        if len(prices) < period + 1:
            return []
        
        try:
            # TA-Lib을 사용하여 RSI 계산
            prices_array = np.array(prices, dtype=float)
            rsi = talib.RSI(prices_array, timeperiod=period)
            
            # 시점 순서 유지: NaN을 50으로 대체 (RSI 중립값)
            rsi_filled = np.where(np.isnan(rsi), 50, rsi)
            return rsi_filled.tolist()
        except Exception as e:
            logging.error(f"RSI 계산 실패: {e}")
            return []
    
    def _calculate_macd(self, prices, fast_period=12, slow_period=26, signal_period=9):
        """MACD(Moving Average Convergence Divergence) 계산"""
        if len(prices) < slow_period:
            return {'macd_line': [], 'signal_line': [], 'histogram': []}
        
        try:
            # TA-Lib을 사용하여 MACD 계산
            prices_array = np.array(prices, dtype=float)
            macd_line, signal_line, histogram = talib.MACD(prices_array, 
                                                          fastperiod=fast_period, 
                                                          slowperiod=slow_period, 
                                                          signalperiod=signal_period)
            
            # 시점 순서 유지: NaN을 0으로 대체
            macd_filled = np.where(np.isnan(macd_line), 0, macd_line)
            signal_filled = np.where(np.isnan(signal_line), 0, signal_line)
            hist_filled = np.where(np.isnan(histogram), 0, histogram)
            
            return {
                'macd_line': macd_filled.tolist(),
                'signal_line': signal_filled.tolist(),
                'histogram': hist_filled.tolist()
            }
        except Exception as e:
            logging.error(f"MACD 계산 실패: {e}")
            return {'macd_line': [], 'signal_line': [], 'histogram': []}



class StockNameMapper:
    """종목명 매핑 클래스 - 중앙화된 종목명 관리"""
    
    _STOCK_MAPPING = {
        "005930": "삼성전자",
        "005380": "현대차",
        "000660": "SK하이닉스", 
        "035420": "NAVER",
        "207940": "삼성바이오로직스",
        "006400": "삼성SDI",
        "051910": "LG화학",
        "035720": "카카오",
        "068270": "셀트리온",
        "323410": "카카오뱅크",
        "000270": "기아"
    }
    
    @classmethod
    def get_stock_name(cls, code):
        """종목코드로 종목명 조회"""
        return cls._STOCK_MAPPING.get(code, f"종목({code})")
    
    @classmethod
    def add_stock_mapping(cls, code, name):
        """새로운 종목 매핑 추가"""
        cls._STOCK_MAPPING[code] = name

class ChartStateManager:
    """차트 상태 관리 클래스"""
    
    def __init__(self):
        self._is_processing = False
        self._processing_code = None
        self._html_generating = None
    
    def start_processing(self, code):
        """처리 시작"""
        if self._is_processing and self._processing_code == code:
            return False  # 이미 처리 중
        elif self._is_processing and self._processing_code != code:
            logging.info(f"📊 다른 차트({self._processing_code})를 생성 중입니다. 이전 작업을 중단하고 새 작업 시작.")
            self._is_processing = False
            self._processing_code = None
        
        self._is_processing = True
        self._processing_code = code
        return True
    
    def finish_processing(self):
        """처리 완료"""
        self._is_processing = False
        self._processing_code = None
        self._html_generating = None
    
    def start_html_generation(self, code):
        """HTML 생성 시작"""
        if self._html_generating == code:
            return False
        self._html_generating = code
        return True
    
    def finish_html_generation(self):
        """HTML 생성 완료"""
        self._html_generating = None
    
    def is_processing(self, code=None):
        """처리 상태 확인"""
        if code:
            return self._is_processing and self._processing_code == code
        return self._is_processing
    
    def reset(self):
        """상태 초기화"""
        self._is_processing = False
        self._processing_code = None
        self._html_generating = None


class TechnicalIndicators:
    """기술적 지표 계산 클래스"""
    
    @staticmethod
    def calculate_sma(prices, period):
        """단순이동평균(Simple Moving Average) 계산"""
        if len(prices) < period:
            return []
        
        try:
            import talib
            import numpy as np
            prices_array = np.array(prices, dtype=float)
            sma = talib.SMA(prices_array, timeperiod=period)
            sma_filled = np.where(np.isnan(sma), 0, sma)
            return sma_filled.tolist()
        except Exception as e:
            logging.error(f"SMA 계산 실패: {e}")
            return []
    
    @staticmethod
    def calculate_rsi(prices, period=14):
        """RSI(Relative Strength Index) 계산"""
        if len(prices) < period + 1:
            return []
        
        try:
            import talib
            import numpy as np
            prices_array = np.array(prices, dtype=float)
            rsi = talib.RSI(prices_array, timeperiod=period)
            rsi_filled = np.where(np.isnan(rsi), 50, rsi)
            return rsi_filled.tolist()
        except Exception as e:
            logging.error(f"RSI 계산 실패: {e}")
            return []
    
    @staticmethod
    def calculate_macd(prices, fast_period=12, slow_period=26, signal_period=9):
        """MACD(Moving Average Convergence Divergence) 계산"""
        if len(prices) < slow_period:
            return {'macd_line': [], 'signal_line': [], 'histogram': []}
        
        try:
            import talib
            import numpy as np
            prices_array = np.array(prices, dtype=float)
            macd_line, signal_line, histogram = talib.MACD(
                prices_array, 
                fastperiod=fast_period, 
                slowperiod=slow_period, 
                signalperiod=signal_period
            )
            
            macd_filled = np.where(np.isnan(macd_line), 0, macd_line)
            signal_filled = np.where(np.isnan(signal_line), 0, signal_line)
            hist_filled = np.where(np.isnan(histogram), 0, histogram)
            
            return {
                'macd_line': macd_filled.tolist(),
                'signal_line': signal_filled.tolist(),
                'histogram': hist_filled.tolist()
            }
        except Exception as e:
            logging.error(f"MACD 계산 실패: {e}")
            return {'macd_line': [], 'signal_line': [], 'histogram': []}


class ChartHTMLGenerator:
    """차트 HTML 생성 클래스"""
    
    @staticmethod
    def generate_empty_chart_html():
        """빈 차트 HTML 생성"""
        return """
        <html>
        <head>
            <title>차트 준비 중</title>
            <style>
                body { 
                    font-family: Arial, sans-serif; 
                    text-align: center; 
                    padding: 50px; 
                    background-color: #f5f5f5;
                }
                .placeholder {
                    color: #666;
                    font-size: 18px;
                }
            </style>
        </head>
        <body>
            <div class="placeholder">차트를 선택해주세요</div>
        </body>
        </html>
        """
    
    @staticmethod
    def generate_error_chart_html(error_msg):
        """에러 메시지 차트 HTML 생성"""
        try:
            import plotly.graph_objects as go
            import plotly.offline as pyo
            
            fig = go.Figure()
            fig.add_annotation(
                x=0.5, y=0.5,
                text=f"[오류] {error_msg}",
                showarrow=False,
                font=dict(size=16, color="red"),
                xref="paper", yref="paper",
                xanchor="center", yanchor="middle"
            )
            fig.update_layout(
                title="API 연결 오류",
                xaxis_title="시간",
                yaxis_title="가격",
                template="plotly_white",
                height=600
            )
            
            return pyo.plot(fig, output_type='div', include_plotlyjs=True)
        except Exception as ex:
            logging.error(f"에러 차트 HTML 생성 실패: {ex}")
            return f"<html><body><h2>오류: {error_msg}</h2></body></html>"
    
    @staticmethod
    def generate_initial_chart_html():
        """초기 차트 HTML 생성"""
        try:
            import plotly.graph_objects as go
            import plotly.offline as pyo
            
            fig = go.Figure()
            fig.add_annotation(
                x=0.5, y=0.5,
                text="📊 차트 영역<br><br>모니터링 종목을 클릭하면<br>실시간 차트가 표시됩니다.<br><br>💡 API 연결 후 사용 가능",
                showarrow=False,
                font=dict(size=14, color="gray"),
                xref="paper", yref="paper",
                xanchor="center", yanchor="middle"
            )
            fig.update_layout(
                title="Stock Chart",
                xaxis_title="시간",
                yaxis_title="가격",
                template="plotly_white",
                height=600,
                showlegend=False
            )
            
            return pyo.plot(fig, output_type='div', include_plotlyjs=True)
        except Exception as ex:
            logging.error(f"초기 차트 HTML 생성 실패: {ex}")
            return ChartHTMLGenerator.generate_empty_chart_html()


class ChartDrawer(QObject):
    """차트 그리기 클래스 (Plotly 기반) - 리팩토링된 버전"""
    
    # 시그널 정의
    chart_finished = pyqtSignal(str)  # HTML 문자열 전달
    chart_error = pyqtSignal(str)     # 에러 메시지 전달
    
    def __init__(self, chart_browser, window):
        super().__init__()
        self.chart_browser = chart_browser
        self.window = window
        self.code = None
        self.name = None
        self.data = None
        
        # 상태 관리자 초기화
        self.state_manager = ChartStateManager()
        
        # 시그널 연결
        self.chart_finished.connect(self._on_chart_finished)
        self.chart_error.connect(self._on_chart_error)
    
    def clear_chart(self):
        """차트 영역을 정리합니다"""
        try:
            # 상태 초기화
            self.state_manager.reset()
            logging.info("📊 차트 처리 상태 초기화 완료")
            
            # 차트 브라우저 내용 초기화
            if self.chart_browser:
                empty_html = ChartHTMLGenerator.generate_empty_chart_html()
                self.chart_browser.setHtml(empty_html)
                logging.info("📊 차트 브라우저 초기화 완료")
                
        except Exception as e:
            logging.error(f"❌ 차트 정리 실패: {e}")
    

    def set_code(self, code, name=None):
        """종목코드 및 종목명 설정 및 차트 업데이트"""
        logging.info(f"ChartDrawer.set_code received name: {name}")

        # 처리 상태 확인 및 설정
        if not self.state_manager.start_processing(code):
            logging.warning(f"📊 이미 {code} 차트를 생성 중입니다. 중복 실행 방지.")
            return
        
        self.code = code
        self.name = name or StockNameMapper.get_stock_name(code)

        if code:
            self._create_chart_from_cache(code, name)
        else:
            self.create_initial_chart()
    
    def _create_chart_from_cache(self, code, name=None):
        """캐시된 데이터로 차트 생성"""
        try:
            # ChartDataCache에서 데이터 확인
            chart_cache = getattr(self.window, 'chart_cache', None) if hasattr(self, 'window') else None
            if not chart_cache:
                self._show_error("차트 데이터 캐시가 초기화되지 않았습니다.")
                return
            
            cached_data = chart_cache.get_chart_data(code)
            if not cached_data or not cached_data.get('tick_data') or not cached_data.get('min_data'):
                self._show_error(f"종목 {code}의 차트 데이터가 메모리에 없습니다. 먼저 모니터링을 시작해주세요.")
                return
            
            # 데이터 유효성 검사
            tick_data = cached_data.get('tick_data')
            min_data = cached_data.get('min_data')
            
            if not self._validate_chart_data(tick_data, min_data, code):
                return
            
            logging.info(f"📊 캐시된 데이터로 차트 생성 시작: {code}")
            logging.info(f"   - 틱 데이터: {len(tick_data.get('close', []))}개")
            logging.info(f"   - 분봉 데이터: {len(min_data.get('close', []))}개")
            
            # Plotly 차트 생성
            html_str = self._generate_chart_html(code, cached_data, name)
            self.chart_finished.emit(html_str)
            logging.info(f"📊 차트 생성 완료: {code}")
                
        except Exception as ex:
            logging.error(f"캐시된 데이터 차트 생성 실패: {ex}")
            self._show_error(f"캐시된 데이터 차트 생성 실패: {ex}")
        finally:
            self.state_manager.finish_processing()
    
    def _validate_chart_data(self, tick_data, min_data, code):
        """차트 데이터 유효성 검사"""
        tick_closes = tick_data.get('close', [])
        min_closes = min_data.get('close', [])
        
        if not tick_closes or not min_closes:
            self._show_error(f"종목 {code}의 가격 데이터가 비어있습니다.")
            return False
        
        return True
    
    def _show_error(self, error_msg):
        """에러 메시지 표시"""
        logging.warning(f"📊 {error_msg}")
        error_html = ChartHTMLGenerator.generate_error_chart_html(error_msg)
        self.chart_browser.setHtml(error_html)
    
    
    def _generate_chart_html(self, code, data, name=None):
        """Plotly 차트 HTML 생성"""
        try:
            logging.info(f"📊 _generate_chart_html 시작: {code}")
            
            # HTML 생성 상태 확인
            if not self.state_manager.start_html_generation(code):
                logging.info(f"📊 {code} HTML이 이미 생성 중입니다. 이전 작업을 중단하고 새 작업 시작.")
            
            # Plotly 라이브러리 확인
            try:
                import plotly.graph_objects as go
                import plotly.offline as pyo
                from plotly.subplots import make_subplots
                logging.info("✅ Plotly 라이브러리 로드 성공")
            except ImportError as e:
                logging.error(f"❌ Plotly 라이브러리 로드 실패: {e}")
                raise e
            
            # 종목명 조회
            stock_name = name or StockNameMapper.get_stock_name(code)
            logging.info(f"📊 종목명: {stock_name}")
            
            # 서브플롯 생성 (2행 1열)
            fig = make_subplots(
                rows=2, cols=1,
                subplot_titles=[f"30틱봉 - {stock_name} (SMA5, SMA20 포함)", f"3분봉 - {stock_name} (SMA5, SMA20 포함)"],
                vertical_spacing=0.1,
                row_heights=[0.5, 0.5]
            )
            logging.info("✅ 서브플롯 생성 완료")
            
            # 틱 차트 그리기 (상단)
            logging.info(f"📊 틱 차트 그리기 시작: {len(data['tick_data'].get('close', []))}개 데이터")
            self._draw_candlestick_plotly(fig, data['tick_data'], row=1, col=1, display_count=70)
            
            # 분봉 차트 그리기 (하단)
            logging.info(f"📊 분봉 차트 그리기 시작: {len(data['min_data'].get('close', []))}개 데이터")
            self._draw_candlestick_plotly(fig, data['min_data'], row=2, col=1, display_count=50)
            
            # 레이아웃 설정
            self._configure_chart_layout(fig, stock_name, code)
            
            # HTML로 변환
            html_str = self._convert_figure_to_html(fig, code)
            logging.info(f"📊 Plotly HTML 변환 완료: {code} (길이: {len(html_str)}자)")
            return html_str
            
        except Exception as ex:
            logging.error(f"차트 HTML 생성 실패: {ex}")
            raise ex
        finally:
            self.state_manager.finish_html_generation()
    
    def _configure_chart_layout(self, fig, stock_name, code):
        """차트 레이아웃 설정"""
        fig.update_layout(
            title=f"{stock_name} ({code}) - 틱봉 & 분봉 차트",
            template="plotly_white",
            height=800,
            showlegend=True,
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1
            )
        )
        
        # x축 시간 형식 설정
        fig.update_xaxes(tickformat="%H:%M", tickangle=45, row=1, col=1)
        fig.update_xaxes(tickformat="%H:%M", tickangle=45, row=2, col=1)
    
    def _convert_figure_to_html(self, fig, code):
        """Plotly Figure를 HTML로 변환"""
        import plotly.offline as pyo
        
        return pyo.plot(
            fig, 
            output_type='div', 
            include_plotlyjs='cdn',  # CDN 사용으로 크기 줄이기
            config={
                'displayModeBar': True,
                'displaylogo': False,
                'modeBarButtonsToRemove': ['pan2d', 'lasso2d', 'select2d'],
                'responsive': True
            }
        )
    
    def _draw_candlestick_plotly(self, fig, data, row, col, display_count=None):
        """Plotly 캔들스틱 차트 그리기"""
        try:
            if not self._validate_candlestick_data(data):
                return
            
            # 데이터 준비
            processed_data = self._prepare_candlestick_data(data, display_count)
            if not processed_data:
                return
            
            # 캔들스틱 차트 추가
            self._add_candlestick_trace(fig, processed_data, row, col)
            
            # 이동평균선 추가
            self._add_moving_averages(fig, processed_data, row, col)
            
        except Exception as ex:
            logging.error(f"캔들스틱 차트 그리기 실패: {ex}")
    
    def _validate_candlestick_data(self, data):
        """캔들스틱 데이터 유효성 검사"""
        return data and data.get('close') and len(data['close']) > 0
    
    def _prepare_candlestick_data(self, data, display_count):
        """캔들스틱 데이터 준비"""
        closes = data['close']
        times = data.get('time', [])
        opens = data.get('open', [])
        highs = data.get('high', [])
        lows = data.get('low', [])
        
        # 최근 데이터만 표시
        if display_count and len(closes) > display_count:
            start_idx = len(closes) - display_count
            closes = closes[start_idx:]
            times = times[start_idx:] if times else []
            opens = opens[start_idx:] if opens else []
            highs = highs[start_idx:] if highs else []
            lows = lows[start_idx:] if lows else []
        
        # 시간 문자열 생성
        time_strings = self._format_time_strings(times)
        
        return {
            'closes': closes,
            'times': time_strings,
            'opens': opens,
            'highs': highs,
            'lows': lows
        }
    
    def _format_time_strings(self, times):
        """시간 문자열 포맷팅"""
        time_strings = []
        for time_val in times:
            if hasattr(time_val, 'strftime'):
                time_strings.append(time_val.strftime('%H:%M:%S'))
            else:
                time_strings.append(str(time_val))
        return time_strings
    
    def _add_candlestick_trace(self, fig, data, row, col):
        """캔들스틱 트레이스 추가"""
        import plotly.graph_objects as go
        
        chart_type = "틱봉" if row == 1 else "분봉"
        chart_name = f"Price ({chart_type}, {len(data['closes'])}개)"
        
        logging.info(f"📊 캔들스틱 차트 추가: {chart_name}, 위치: row={row}, col={col}")
        
        fig.add_trace(
            go.Candlestick(
                x=data['times'],
                open=data['opens'],
                high=data['highs'],
                low=data['lows'],
                close=data['closes'],
                name=chart_name,
                showlegend=True
            ),
            row=row, col=col
        )
    
    def _add_moving_averages(self, fig, data, row, col):
        """이동평균선 추가"""
        import plotly.graph_objects as go
        
        chart_type = "틱봉" if row == 1 else "분봉"
        closes = data['closes']
        times = data['times']
        
        # SMA5 추가
        if len(closes) >= 5:
            sma5 = TechnicalIndicators.calculate_sma(closes, 5)
            if sma5:
                self._add_sma_trace(fig, sma5, times, f'SMA5 ({chart_type})', 'orange', row, col, 4)
        
        # SMA20 추가
        if len(closes) >= 20:
            sma20 = TechnicalIndicators.calculate_sma(closes, 20)
            if sma20:
                self._add_sma_trace(fig, sma20, times, f'SMA20 ({chart_type})', 'blue', row, col, 19)
    
    def _add_sma_trace(self, fig, sma, times, name, color, row, col, start_idx):
        """SMA 트레이스 추가"""
        import plotly.graph_objects as go
        
        sma_indices = list(range(start_idx, len(times)))
        sma_times = [times[i] for i in sma_indices]
        
        fig.add_trace(
            go.Scatter(
                x=sma_times,
                y=sma[start_idx:],
                mode='lines',
                name=name,
                line=dict(color=color, width=1),
                showlegend=True
            ),
            row=row, col=col
        )
    
    
    @pyqtSlot(dict)
    def _on_data_ready(self, data):
        """차트 데이터 준비 완료 시그널 처리 (사용하지 않음 - 메모리 데이터만 사용)"""
        logging.warning("_on_data_ready 메서드가 호출되었지만 사용되지 않습니다.")
    
    def _on_chart_finished(self, html_str):
        """차트 생성 완료 시그널 처리"""
        try:
            logging.info(f"📊 차트 표시 시작 - HTML 길이: {len(html_str)}자")
            
            # 차트 브라우저 표시
            self._show_chart_browser()
            
            # HTML 설정
            if hasattr(self.chart_browser, 'setHtml'):
                self._set_chart_html(html_str)
            else:
                logging.error("❌ 차트 브라우저가 HTML을 지원하지 않습니다")
                self._show_error("차트 브라우저 오류")
            
        except Exception as ex:
            logging.error(f"❌ 차트 표시 실패: {ex}")
            self._show_error(f"차트 표시 실패: {ex}")
        finally:
            self.state_manager.finish_processing()
    
    def _on_chart_error(self, error_msg):
        """차트 생성 에러 시그널 처리"""
        logging.error(f"차트 생성 에러: {error_msg}")
        self._show_error(error_msg)
        self.state_manager.finish_processing()
    
    def _show_chart_browser(self):
        """차트 브라우저 표시"""
        if not self.chart_browser.isVisible():
            logging.info("📊 차트 브라우저가 숨겨져 있음 - 표시 중...")
            self.chart_browser.setVisible(True)
            self.chart_browser.show()
            self.chart_browser.raise_()
    
    def _set_chart_html(self, html_str):
        """차트 HTML 설정"""
        try:
            # HTML 내용 검증
            if not html_str or len(html_str) < 100:
                logging.error("❌ HTML 내용이 비어있거나 너무 짧습니다")
                return
            
            # 완전한 HTML 문서로 래핑
            full_html = self._wrap_chart_html(html_str)
            self.chart_browser.setHtml(full_html)
            
            # 로딩 상태 확인
            self._check_loading_status()
            
            logging.info("✅ WebEngine HTML 설정 완료")
            
        except Exception as e:
            logging.error(f"❌ WebEngine HTML 설정 실패: {e}")
            self._set_fallback_html(html_str)
    
    def _wrap_chart_html(self, html_str):
        """차트 HTML을 완전한 HTML 문서로 래핑"""
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>차트 - {self.code}</title>
            <style>
                body {{
                    margin: 0;
                    padding: 10px;
                    font-family: Arial, sans-serif;
                    background-color: #f5f5f5;
                }}
                .chart-container {{
                    background-color: white;
                    border-radius: 8px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                    padding: 10px;
                }}
            </style>
        </head>
        <body>
            <div class="chart-container">
                <h3>종목: {self.code}</h3>
                {html_str}
            </div>
            <script>
                setTimeout(function() {{
                    console.log('차트 로딩 완료');
                }}, 2000);
            </script>
        </body>
        </html>
        """
    
    def _set_fallback_html(self, html_str):
        """폴백 HTML 설정"""
        simple_html = f"""
        <html>
        <head><title>차트</title></head>
        <body>
            <h2>차트 로딩 중...</h2>
            <p>종목: {self.code}</p>
            <p>HTML 길이: {len(html_str)}자</p>
        </body>
        </html>
        """
        self.chart_browser.setHtml(simple_html)
    
    def _check_loading_status(self):
        """로딩 상태 확인"""
        from PyQt6.QtCore import QTimer
        
        def check_loading():
            try:
                if hasattr(self.chart_browser, 'page'):
                    page = self.chart_browser.page()
                    if hasattr(page, 'isLoading'):
                        if not page.isLoading():
                            logging.info("✅ WebEngine 페이지 로딩 완료")
                        else:
                            logging.info("⏳ WebEngine 페이지 로딩 중...")
            except Exception as e:
                logging.warning(f"⚠️ 로딩 상태 확인 실패: {e}")
        
        QTimer.singleShot(1000, check_loading)

    def create_initial_chart(self):
        """초기 차트 생성"""
        try:
            html_str = ChartHTMLGenerator.generate_initial_chart_html()
            if hasattr(self.chart_browser, 'setHtml'):
                self.chart_browser.setHtml(html_str)
            else:
                logging.error("차트 브라우저가 HTML을 지원하지 않습니다")
        except Exception as ex:
            logging.error(f"초기 차트 생성 실패: {ex}")
            self._show_error("초기 차트 생성 실패")


# ==================== 웹소켓 연결 QThread 클래스 ====================
# WebSocketConnectionThread 클래스 제거됨 - asyncio만 사용

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
            logging.info(f"기술적지표 계산 시작 - 전체 데이터: {len(closes)}개")
            
            sma5 = TechnicalIndicators.calculate_sma(closes, 5) if len(closes) >= 5 else []
            logging.info(f"SMA5 계산 완료: {len(sma5)}개 (필요: 5개 이상)")
            
            sma20 = TechnicalIndicators.calculate_sma(closes, 20) if len(closes) >= 20 else []
            logging.info(f"SMA20 계산 완료: {len(sma20)}개 (필요: 20개 이상)")
            
            rsi = TechnicalIndicators.calculate_rsi(closes, 14) if len(closes) >= 15 else []
            logging.info(f"RSI 계산 완료: {len(rsi)}개 (필요: 15개 이상)")
            
            macd_result = TechnicalIndicators.calculate_macd(closes) if len(closes) >= 26 else {'macd_line': [], 'signal_line': [], 'histogram': []}
            macd_line, signal_line, histogram = macd_result.get('macd_line', []), macd_result.get('signal_line', []), macd_result.get('histogram', [])
            logging.info(f"MACD 계산 완료: MACD={len(macd_line)}개, Signal={len(signal_line)}개, Hist={len(histogram)}개 (필요: 26개 이상)")
            
            # 최근 10개 데이터만 표시
            display_count = min(10, len(closes))
            start_idx = len(closes) - display_count
            
            times = times[start_idx:]
            opens = opens[start_idx:]
            highs = highs[start_idx:]
            lows = lows[start_idx:]
            closes = closes[start_idx:]
            
            # 표 헤더 출력
            logging.info("=" * 120)
            logging.info(f"📊 {title} OHLC & 기술적지표 분석표")
            logging.info("=" * 120)
            logging.info(f"{'시간':<8} {'시가':<8} {'고가':<8} {'저가':<8} {'종가':<8} {'SMA5':<8} {'SMA20':<8} {'RSI':<6} {'MACD':<8} {'Signal':<8} {'Hist':<8}")
            logging.info("-" * 120)
            
            # 각 시점별 데이터 출력
            for i in range(len(closes)):
                time_str = times[i].strftime('%H:%M:%S') if hasattr(times[i], 'strftime') else str(times[i])[-8:]
                
                # 전체 데이터에서의 실제 인덱스 계산 (표시 시작점 + 현재 인덱스)
                actual_idx = start_idx + i
                
                # 기술적지표 값 계산 (이제 모든 지표가 원본 데이터와 같은 길이)
                sma5_val = ""
                if sma5 and len(sma5) > actual_idx:
                    sma5_val = sma5[actual_idx]
                
                sma20_val = ""
                if sma20 and len(sma20) > actual_idx:
                    sma20_val = sma20[actual_idx]
                
                rsi_val = ""
                if rsi and len(rsi) > actual_idx:
                    rsi_val = rsi[actual_idx]
                
                macd_val = ""
                signal_val = ""
                hist_val = ""
                if macd_line and signal_line and histogram and len(macd_line) > actual_idx:
                    macd_val = macd_line[actual_idx]
                    signal_val = signal_line[actual_idx]
                    hist_val = histogram[actual_idx]
                
                # 값 포맷팅 (0 값은 유효하지 않은 값으로 표시)
                sma5_str = f"{sma5_val:,.0f}" if sma5_val != "" and sma5_val != 0 else "   -   "
                sma20_str = f"{sma20_val:,.0f}" if sma20_val != "" and sma20_val != 0 else "   -   "
                rsi_str = f"{rsi_val:.1f}" if rsi_val != "" and rsi_val != 50 else "  -  "
                macd_str = f"{macd_val:.2f}" if macd_val != "" and macd_val != 0 else "   -   "
                signal_str = f"{signal_val:.2f}" if signal_val != "" and signal_val != 0 else "   -   "
                hist_str = f"{hist_val:.2f}" if hist_val != "" and hist_val != 0 else "   -   "
                
                logging.info(f"{time_str:<8} {opens[i]:<8,.0f} {highs[i]:<8,.0f} {lows[i]:<8,.0f} {closes[i]:<8,.0f} {sma5_str:<8} {sma20_str:<8} {rsi_str:<6} {macd_str:<8} {signal_str:<8} {hist_str:<8}")
            
            # 요약 정보 출력 (전체 데이터 기준)
            logging.info("-" * 120)
            current_price = closes[-1]
            logging.info(f"현재가: {current_price:,.0f}")
            logging.info(f"전체 데이터 수: {len(data['close'])}개")
            
            # 유효한 기술적지표 값만 표시
            if sma5 and len(sma5) > 0 and sma5[-1] != 0:
                logging.info(f"SMA5: {sma5[-1]:,.0f} (차이: {current_price - sma5[-1]:+,.0f})")
            if sma20 and len(sma20) > 0 and sma20[-1] != 0:
                logging.info(f"SMA20: {sma20[-1]:,.0f} (차이: {current_price - sma20[-1]:+,.0f})")
            if rsi and len(rsi) > 0 and rsi[-1] != 50:
                rsi_value = rsi[-1]
                rsi_status = "과매수" if rsi_value > 70 else "과매도" if rsi_value < 30 else "중립"
                logging.info(f"RSI: {rsi_value:.2f} ({rsi_status})")
            if macd_line and signal_line and len(macd_line) > 0 and len(signal_line) > 0 and macd_line[-1] != 0:
                macd_signal = "상승" if macd_line[-1] > signal_line[-1] else "하락"
                logging.info(f"MACD: {macd_line[-1]:.2f} vs Signal: {signal_line[-1]:.2f} ({macd_signal})")
            
            logging.info("=" * 120)
            
        except Exception as ex:
            logging.error(f"OHLC 표 출력 실패: {ex}")

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
        self.execution_data = {}  # 주식체결 데이터 저장
        self.market_status = {}  # 시장 상태 데이터 저장
        self._connecting = False  # 중복 연결 방지 플래그
        self._connection_lock = asyncio.Lock()  # 연결 락
        self.parent = parent  # 부모 윈도우 참조
        
    async def connect(self):
        """웹소켓 연결 (키움증권 예시코드 기반)"""
        try:
            mode_text = "모의투자" if self.is_mock else "실제투자"
            self.logger.info(f"🔧 웹소켓 연결 시작... ({mode_text})")
            self.logger.info(f"🔧 웹소켓 서버: {self.uri}")
            
            # 웹소켓 연결 (키움증권 예시코드와 동일)
            self.websocket = await websockets.connect(self.uri, ping_interval=None)
            self.connected = True
            
            self.logger.info("✅ 웹소켓 서버와 연결을 시도 중입니다.")
            
            # 로그인 패킷 (키움증권 예시코드 구조)
            login_param = {
                'trnm': 'LOGIN',
                'token': self.token
            }
            
            self.logger.info('🔧 실시간 시세 서버로 로그인 패킷을 전송합니다.')
            # 웹소켓 연결 시 로그인 정보 전달
            await self.send_message(login_param)
            
            return True
            
        except Exception as e:
            self.logger.error(f'❌ 웹소켓 연결 오류: {e}')
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
                self.logger.info('✅ 웹소켓 서버와 연결이 해제되었습니다')
            
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
            self.execution_data.clear()
            self.market_status.clear()
            
            self.logger.info('✅ 웹소켓 클라이언트 완전 정리 완료')
            
        except Exception as ex:
            self.logger.error(f"❌ 웹소켓 연결 해제 실패: {ex}")
            import traceback
            self.logger.error(f"웹소켓 해제 에러 상세: {traceback.format_exc()}")
    
    async def run(self):
        """웹소켓 클라이언트 실행 (키움증권 예시코드 기반)"""
        try:
            # 서버에 연결하고, 메시지를 계속 받을 준비를 합니다.
            await self.connect()
            await self.receive_messages()
            
        except asyncio.CancelledError:
            self.logger.info("🛑 웹소켓 클라이언트 태스크가 취소되었습니다")
            raise  # CancelledError는 다시 발생시켜야 함
        except Exception as e:
            self.logger.error(f"❌ 웹소켓 클라이언트 실행 중 오류: {e}")
            import traceback
            self.logger.error(f"웹소켓 실행 에러 상세: {traceback.format_exc()}")
        finally:
            self.logger.info("🔌 웹소켓 클라이언트 정리 중...")
            await self.disconnect()
            self.logger.info("✅ 웹소켓 클라이언트 정리 완료")

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
                    self.logger.info(f'메시지 전송: {message}')
            except (json.JSONDecodeError, AttributeError):
                # JSON 파싱 실패시 기본 로그 출력
                self.logger.info(f'메시지 전송: {message}')

    async def receive_messages(self):
        """서버에서 메시지 수신"""
        self.logger.info("🔧 웹소켓 메시지 수신 루프 시작")
        message_count = 0
        
        while self.keep_running and self.connected:
            try:
                # 서버로부터 수신한 메시지를 JSON 형식으로 파싱
                self.logger.debug(f"🔧 메시지 수신 대기 중... (수신된 메시지 수: {message_count})")
                message = await self.websocket.recv()
                message_count += 1
                self.logger.info(f"📨 메시지 수신 완료 (총 {message_count}개): {message}")

                response = json.loads(message)

                # 메시지 유형이 LOGIN일 경우 로그인 시도 결과 체크 (키움증권 예시코드 기반)
                if response.get('trnm') == 'LOGIN':
                    if response.get('return_code') != 0:
                        self.logger.error('❌ 웹소켓 로그인 실패하였습니다. : ', response.get('return_msg'))
                        await self.disconnect()
                    else:
                        mode_text = "모의투자" if self.is_mock else "실제투자"
                        self.logger.info(f'✅ 웹소켓 로그인 성공하였습니다. ({mode_text} 모드)')
                        
                        # 웹소켓 연결 성공 로그
                        self.logger.info("✅ 웹소켓 연결 성공 - UI 상태 업데이트는 제거됨")
                        
                        # 웹소켓 연결 성공 시 post_login_setup 실행
                        try:
                            import asyncio
                            async def delayed_post_login_setup():
                                await asyncio.sleep(1.0)  # 1초 대기
                                # 부모 윈도우의 post_login_setup 메서드 호출 (async)
                                if hasattr(self, 'parent') and hasattr(self.parent, 'post_login_setup'):
                                    await self.parent.post_login_setup()
                                    self.logger.info("✅ post_login_setup 실행 완료")
                                else:
                                    self.logger.warning("⚠️ post_login_setup 메서드를 찾을 수 없습니다")
                            asyncio.create_task(delayed_post_login_setup())
                            self.logger.info("📋 post_login_setup 실행 예약 (1초 후)")
                        except Exception as setup_err:
                            self.logger.error(f"❌ post_login_setup 실행 실패: {setup_err}")
                        
                        # 로그인 성공 후 시장 상태 구독 시작
                        try:
                            await self.subscribe_market_status()
                            self.logger.info("🔔 시장 상태 모니터링 시작")
                        except Exception as market_sub_err:
                            self.logger.error(f"❌ 시장 상태 구독 실패: {market_sub_err}")

                # 메시지 유형이 PING일 경우 수신값 그대로 송신 (키움증권 예시코드 기반)
                if response.get('trnm') == 'PING':
                    await self.send_message(response)

                # 키움증권 예시코드 방식: PING이 아닌 모든 응답을 로그로 출력
                if response.get('trnm') != 'PING':
                    self.logger.info(f'📡 실시간 시세 서버 응답 수신: {response}')
                    
                    # REG 응답인 경우 구독 성공 확인
                    if response.get('trnm') == 'REG':
                        if response.get('return_code') == 0:
                            self.logger.info('✅ 시장 상태 구독 성공! 실시간 데이터 대기 중...')
                        else:
                            self.logger.error(f'❌ 시장 상태 구독 실패: {response.get("return_msg")}')
                    
                    # CNSRLST 응답인 경우 조건검색 목록조회 결과 처리
                    if response.get('trnm') == 'CNSRLST':
                        try:
                            self.process_condition_search_list_response(response)
                        except Exception as condition_err:
                            self.logger.error(f"❌ 조건검색 목록조회 응답 처리 실패: {condition_err}")
                            import traceback
                            self.logger.error(f"조건검색 응답 처리 에러 상세: {traceback.format_exc()}")

                # 실시간 데이터 처리
                if response.get('trnm') == 'REAL':  # 실시간 데이터
                    
                    # 실시간 데이터 처리 (예외 처리 강화)
                    try:
                        data_list = response.get('data', [])
                        if not isinstance(data_list, list):
                            self.logger.warning(f"실시간 데이터가 리스트가 아닙니다: {type(data_list)}")
                            continue
                            
                        for data_item in data_list:
                            try:
                                if not isinstance(data_item, dict):
                                    self.logger.warning(f"데이터 아이템이 딕셔너리가 아닙니다: {type(data_item)}")
                                    continue
                                    
                                data_type = data_item.get('type')
                                if data_type == '04':  # 현물잔고
                                    self.logger.info("실시간 잔고 정보 수신")
                                    try:
                                        self.process_balance_data(data_item)
                                    except Exception as balance_err:
                                        self.logger.error(f"잔고 데이터 처리 실패: {balance_err}")
                                        import traceback
                                        self.logger.error(f"잔고 데이터 처리 에러 상세: {traceback.format_exc()}")
                                elif data_type == '0A':  # 주식 시세
                                    self.logger.debug(f"실시간 주식 시세 수신: {data_item.get('item')}")
                                elif data_type == '0B':  # 주식체결
                                    self.logger.info(f"실시간 주식체결 수신: {data_item.get('item')}")
                                    try:
                                        self.process_stock_execution_data(data_item)
                                    except Exception as execution_err:
                                        self.logger.error(f"체결 데이터 처리 실패: {execution_err}")
                                        import traceback
                                        self.logger.error(f"체결 데이터 처리 에러 상세: {traceback.format_exc()}")
                                elif data_type == '0s':  # 시장 상태
                                    self.logger.info(f"실시간 시장 상태 수신: {data_item.get('item')}")
                                    try:
                                        self.process_market_status_data(data_item)
                                    except Exception as market_err:
                                        self.logger.error(f"시장 상태 데이터 처리 실패: {market_err}")
                                        import traceback
                                        self.logger.error(f"시장 상태 데이터 처리 에러 상세: {traceback.format_exc()}")
                                else:
                                    self.logger.debug(f"알 수 없는 실시간 데이터 타입: {data_type}")
                            except Exception as data_item_err:
                                self.logger.error(f"실시간 데이터 아이템 처리 실패: {data_item_err}")
                                import traceback
                                self.logger.error(f"데이터 아이템 처리 에러 상세: {traceback.format_exc()}")
                                continue
                        
                        # 메시지 큐에 추가 (예외 처리)
                        try:
                            self.message_queue.put(response)
                        except Exception as queue_err:
                            self.logger.error(f"메시지 큐 추가 실패: {queue_err}")
                            
                    except Exception as data_process_err:
                        self.logger.error(f"실시간 데이터 처리 실패: {data_process_err}")
                        import traceback
                        self.logger.error(f"실시간 데이터 처리 에러 상세: {traceback.format_exc()}")
                        continue
                
                # 조건검색 응답 처리 (일반 요청 및 실시간 알림)
                elif response.get('trnm') == 'CNSRREQ':  # 조건검색 응답
                    self.logger.info(f"조건검색 응답 수신: {response}")
                    try:
                        # 응답 타입에 따라 분기 처리
                        search_type = response.get('search_type', '0')
                        if search_type == '0':  # 일반 요청 응답
                            self.logger.info("조건검색 일반 요청 응답 처리")
                            self.process_condition_search_response(response)
                        elif search_type == '1':  # 실시간 요청 응답
                            self.logger.info("조건검색 실시간 요청 응답 처리")
                            self.process_condition_search_response(response)
                        else:  # 실시간 알림 응답
                            self.logger.info("조건검색 실시간 알림 응답 처리")
                            self.process_condition_realtime_notification(response)
                    except Exception as condition_err:
                        self.logger.error(f"조건검색 응답 처리 실패: {condition_err}")
                        import traceback
                        self.logger.error(f"조건검색 응답 처리 에러 상세: {traceback.format_exc()}")
                elif response.get('trnm') == '0A':  # 실시간 주식 시세 (기존 호환성)
                    self.message_queue.put(response)
                    if response.get('trnm') != 'PING':
                        self.logger.debug(f'실시간 데이터 수신: {response}')

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
                import traceback
                self.logger.error(f'메시지 수신 에러 상세: {traceback.format_exc()}')
                # 연결 종료 대신 계속 시도 (일시적 오류일 수 있음)
                self.logger.warning("메시지 수신 오류 발생, 연결 유지하고 계속 시도")
                
                # 심각한 오류인 경우 잠시 대기
                try:
                    await asyncio.sleep(1)  # 1초 대기
                except Exception as sleep_err:
                    self.logger.error(f"대기 중 오류: {sleep_err}")
                
                continue

    async def subscribe_realtime_data(self, codes=None, subscription_type='monitoring'):
        """실시간 데이터 구독"""
        if codes is None:
            codes = list(self.subscribed_codes)
            
        if codes:
            # 주식 시세 구독 (0A)
            subscribe_data = {
                'trnm': 'REG',  # 서비스명
                'grp_no': '1' if subscription_type == 'monitoring' else '2',  # 그룹번호
                'refresh': '1',  # 기존등록유지여부
                'data': [{  # 실시간 등록 리스트
                    'item': codes,  # 실시간 등록 요소
                    'type': ['0A'],  # 실시간 항목 (주식 시세)
                }]
            }
            await self.send_message(subscribe_data)
            
            # 실시간 구독 요청 로그 중단
            # if subscription_type == 'monitoring':
            #     self.logger.info(f'모니터링 종목 실시간 시세 구독 요청: {codes}')
            # else:
            #     self.logger.info(f'보유 종목 실시간 시세 구독 요청: {codes}')
    
    async def subscribe_stock_execution_data(self, codes=None, subscription_type='monitoring'):
        """실시간 주식체결 데이터 구독 (0B)"""
        if codes is None:
            codes = list(self.subscribed_codes)
            
        if codes:
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
            self.logger.info(f'실시간 주식체결 구독 요청: {codes}')

    async def subscribe_market_status(self):
        """시장 상태 구독 (0s) - 키움증권 예시코드 기반"""
        try:
            # 키움증권 예시코드에 따른 시장 상태 구독
            subscribe_data = {
                'trnm': 'REG',  # 서비스명
                'grp_no': '1',  # 그룹번호
                'refresh': '1',  # 기존등록유지여부
                'data': [{  # 실시간 등록 리스트
                    'item': [''],  # 실시간 등록 요소 (빈 문자열 - 키움 예시코드 방식)
                    'type': ['0s'],  # 실시간 항목 (시장 상태)
                }]
            }

            self.logger.info('🔧 시장 상태 구독 요청 전송 중...')
            
            await self.send_message(subscribe_data)
            self.logger.info('✅ 시장 상태 구독 요청 전송 완료')
            self.logger.info('⏳ 서버 응답 및 실시간 데이터 대기 중...')
            
        except Exception as e:
            self.logger.error(f'❌ 시장 상태 구독 요청 실패: {e}')

    def process_balance_data(self, data_item):
        """실시간 잔고 데이터 처리 (웹소켓용)
        주의: 이 메서드는 웹소켓을 통한 실시간 잔고 데이터를 처리합니다.
        REST API 계좌평가현황과는 별개의 데이터입니다.
        """
        try:
            # 실시간 잔고 데이터를 balance_data에 저장
            item_data = data_item.get('item', {})
            if item_data:
                # 종목코드를 키로 사용하여 잔고 정보 저장
                stock_code = item_data.get('code', '')
                if stock_code:
                    self.balance_data[stock_code] = {
                        'code': stock_code,
                        'name': item_data.get('name', ''),
                        'quantity': int(item_data.get('quantity', 0)),
                        'average_price': float(item_data.get('average_price', 0)),
                        'current_price': float(item_data.get('current_price', 0)),
                        'total_value': float(item_data.get('total_value', 0)),
                        'profit_loss': float(item_data.get('profit_loss', 0)),
                        'profit_loss_rate': float(item_data.get('profit_loss_rate', 0)),
                        'updated_at': datetime.now().isoformat()
                    }
                    self.logger.info(f"실시간 잔고 데이터 업데이트: {stock_code} ({item_data.get('name', '')}) - 수량: {item_data.get('quantity', 0)}주")
                else:
                    self.logger.warning("실시간 잔고 데이터에서 종목코드를 찾을 수 없습니다")
            else:
                self.logger.warning("실시간 잔고 데이터에 item 정보가 없습니다")
                
        except Exception as e:
            self.logger.error(f"실시간 잔고 데이터 처리 실패: {e}")
            import traceback
            self.logger.error(f"잔고 데이터 처리 에러 상세: {traceback.format_exc()}")

    def process_stock_execution_data(self, data_item):
        """실시간 주식체결 데이터 처리"""
        try:
            # 실시간 체결 데이터를 execution_data에 저장
            item_data = data_item.get('item', {})
            if item_data:
                stock_code = item_data.get('code', '')
                if stock_code:
                    self.execution_data[stock_code] = {
                        'code': stock_code,
                        'name': item_data.get('name', ''),
                        'current_price': float(item_data.get('current_price', 0)),
                        'volume': int(item_data.get('volume', 0)),
                        'change': float(item_data.get('change', 0)),
                        'change_rate': float(item_data.get('change_rate', 0)),
                        'high_price': float(item_data.get('high_price', 0)),
                        'low_price': float(item_data.get('low_price', 0)),
                        'open_price': float(item_data.get('open_price', 0)),
                        'updated_at': datetime.now().isoformat()
                    }
                    self.logger.debug(f"실시간 체결 데이터 업데이트: {stock_code} ({item_data.get('name', '')}) - 현재가: {item_data.get('current_price', 0)}원")
                else:
                    self.logger.warning("실시간 체결 데이터에서 종목코드를 찾을 수 없습니다")
            else:
                self.logger.warning("실시간 체결 데이터에 item 정보가 없습니다")
                
        except Exception as e:
            self.logger.error(f"실시간 체결 데이터 처리 실패: {e}")
            import traceback
            self.logger.error(f"체결 데이터 처리 에러 상세: {traceback.format_exc()}")

    def process_condition_realtime_notification(self, response):
        """조건검색 실시간 알림 처리"""
        try:
            # 조건검색 실시간 알림 데이터 처리
            data = response.get('data', [])
            seq = response.get('seq', '')
            
            # data가 리스트인 경우와 딕셔너리인 경우 모두 처리
            if isinstance(data, list):
                # 리스트 형태의 데이터 (일반 요청 응답과 동일한 형태)
                self.logger.info(f"조건검색 실시간 알림 (리스트): 시퀀스={seq}, 종목수={len(data)}")
                
                # 조건검색 결과를 메시지 큐에 추가하여 UI에서 처리할 수 있도록 함
                notification_data = {
                    'type': 'condition_notification',
                    'seq': seq,
                    'action': 'ADD',  # 기본 액션
                    'stock_list': data,
                    'timestamp': datetime.now().isoformat()
                }
                self.message_queue.put(notification_data)
                
            elif isinstance(data, dict):
                # 딕셔너리 형태의 데이터 (실제 실시간 알림)
                action = data.get('action', '')
                stock_list = data.get('stock_list', [])
                
                self.logger.info(f"조건검색 실시간 알림 (딕셔너리): 시퀀스={seq}, 액션={action}, 종목수={len(stock_list)}")
                
                # 조건검색 결과를 메시지 큐에 추가하여 UI에서 처리할 수 있도록 함
                notification_data = {
                    'type': 'condition_notification',
                    'seq': seq,
                    'action': action,
                    'stock_list': stock_list,
                    'timestamp': datetime.now().isoformat()
                }
                self.message_queue.put(notification_data)
            else:
                self.logger.warning(f"알 수 없는 조건검색 실시간 알림 데이터 형태: {type(data)}")
            
        except Exception as e:
            self.logger.error(f"조건검색 실시간 알림 처리 실패: {e}")
            import traceback
            self.logger.error(f"조건검색 알림 처리 에러 상세: {traceback.format_exc()}")

    def get_balance_data(self):
        """웹소켓 실시간 잔고 데이터 조회
        주의: 이 메서드는 웹소켓을 통한 실시간 잔고 데이터를 반환합니다.
        REST API 계좌평가현황과는 별개의 데이터입니다.
        """
        return self.balance_data.copy()

    def get_execution_data(self):
        """웹소켓 실시간 체결 데이터 조회"""
        return self.execution_data.copy()

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
                self.logger.info(f"📋 수신된 데이터: {data_item}")
                return
            
            # 시장 상태 저장
            self.market_status = {
                'market_operation': market_operation,
                'execution_time': execution_time,
                'remaining_time': remaining_time,
                'updated_at': datetime.now().isoformat()
            }
            
            # 시장 상태 상세 정보 로그 출력
            self.logger.info("=" * 60)
            self.logger.info("📊 현재 시장 상태 정보 (API 문서 기반)")
            self.logger.info("=" * 60)
            self.logger.info(f"🔔 장운영구분 (215): {market_operation}")
            self.logger.info(f"⏰ 체결시간 (20): {execution_time}")
            self.logger.info(f"⏳ 장시작예상잔여시간 (214): {remaining_time}")
            self.logger.info("=" * 60)
            
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
                
            # 전체 values 데이터도 로그로 출력
            self.logger.info(f"📋 전체 values 데이터: {values}")
                
        except Exception as e:
            self.logger.error(f"시장 상태 데이터 처리 실패: {e}")
            import traceback
            self.logger.error(f"시장 상태 데이터 처리 에러 상세: {traceback.format_exc()}")

    def get_market_status_data(self):
        """시장 상태 데이터 조회"""
        return self.market_status.copy()
    
    def process_condition_search_list_response(self, response):
        """조건검색 목록조회 응답 처리"""
        try:
            self.logger.info("🔍 조건검색 목록조회 응답 처리 시작")
            
            # 응답 상태 확인
            if response.get('return_code') != 0:
                self.logger.error(f"❌ 조건검색 목록조회 실패: {response.get('return_msg', '알 수 없는 오류')}")
                return
            
            # 조건검색 목록 데이터 추출
            data_list = response.get('data', [])
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
            
            self.logger.info(f"✅ 조건검색 목록조회 성공: {len(condition_list)}개 조건 발견")
            self.logger.info("📋 등록된 조건검색 목록:")
            
            for condition in condition_list:
                self.logger.info(f"  - {condition['title']} (seq: {condition['seq']})")
            
            # 부모 윈도우에 조건검색 목록 전달
            if hasattr(self, 'parent') and self.parent:
                self.parent.condition_search_list = condition_list
                self.logger.info("💾 조건검색 목록을 부모 윈도우에 저장했습니다")
                
                # 투자전략 콤보박스에 조건검색식 추가
                self.logger.info("🔍 투자전략 콤보박스에 조건검색식 추가 시작")
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
                    
                    self.logger.info(f"✅ 조건검색식 목록 로드 완료: {len(condition_list)}개 종목이 투자전략 콤보박스에 추가됨")
                    self.logger.info("📋 이제 투자전략 콤보박스에서 조건검색식을 선택할 수 있습니다")
                    
                    # 저장된 조건검색식이 있는지 확인하고 자동 실행
                    self.logger.info("🔍 저장된 조건검색식 자동 실행 확인 시작")
                    self.parent.check_and_auto_execute_saved_condition()
                    
                    # 저장된 조건검색식이 없으면 첫 번째 조건검색 자동 실행
                    if not self.parent.check_and_auto_execute_saved_condition():
                        self.logger.info("🔍 저장된 조건검색식이 없어 첫 번째 조건검색 자동 실행")
                        if condition_list:
                            first_condition = condition_list[0]
                            condition_seq = first_condition['seq']
                            condition_name = first_condition['title']
                            
                            # 비동기로 조건검색 실행
                            import asyncio
                            async def auto_execute_first_condition():
                                await asyncio.sleep(2.0)  # 2초 대기
                                await self.parent.search_condition_normal(condition_seq)
                                self.logger.info(f"✅ 첫 번째 조건검색 자동 실행 완료: {condition_name} (seq: {condition_seq})")
                            
                            asyncio.create_task(auto_execute_first_condition())
                            self.logger.info(f"🔍 첫 번째 조건검색 자동 실행 예약 (2초 후): {condition_name}")
                    
                except Exception as add_ex:
                    self.logger.error(f"❌ 투자전략 콤보박스에 조건검색식 추가 실패: {add_ex}")
                    import traceback
                    self.logger.error(f"조건검색식 추가 에러 상세: {traceback.format_exc()}")
            
        except Exception as e:
            self.logger.error(f"❌ 조건검색 목록조회 응답 처리 실패: {e}")
            import traceback
            self.logger.error(f"조건검색 응답 처리 에러 상세: {traceback.format_exc()}")
            
            # 오류 발생 시 부모 윈도우에 None 전달
            if hasattr(self, 'parent') and self.parent:
                self.parent.condition_search_list = None

    def process_condition_search_response(self, response):
        """조건검색 일반 요청 응답 처리"""
        try:
            self.logger.info("🔍 조건검색 일반 요청 응답 처리 시작")
            
            # 응답 상태 확인
            if response.get('return_code') != 0:
                self.logger.error(f"❌ 조건검색 일반 요청 실패: {response.get('return_msg', '알 수 없는 오류')}")
                return
            
            # 조건검색 결과 데이터 추출
            data_list = response.get('data', [])
            if not isinstance(data_list, list):
                self.logger.warning(f"⚠️ 조건검색 데이터가 리스트가 아닙니다: {type(data_list)}")
                return
            
            if not data_list:
                self.logger.warning("⚠️ 조건검색 결과가 없습니다")
                return
            
            # 조건검색 결과 처리 (API 문서 기반)
            stock_list = []
            for item in data_list:
                if isinstance(item, dict):
                    # 종목 정보 추출 (API 문서 필드명 사용)
                    code = item.get('9001', '')  # 종목코드
                    name = item.get('302', '')   # 종목명
                    current_price = item.get('10', '')  # 현재가
                    change_rate = item.get('12', '')    # 등락율
                    
                    if code and name:
                        stock_list.append({
                            'code': code,
                            'name': name,
                            'current_price': current_price,
                            'change_rate': change_rate
                        })
                        self.logger.info(f"📋 조건검색 결과: {name} ({code}) - 현재가: {current_price}, 등락율: {change_rate}%")
            
            if stock_list:
                self.logger.info(f"✅ 조건검색 일반 요청 성공: {len(stock_list)}개 종목 발견")
                
                # 부모 윈도우에 조건검색 결과 전달 및 모니터링 종목에 추가
                if hasattr(self, 'parent') and self.parent:
                    # 조건검색 결과를 모니터링 종목에 추가
                    added_count = 0
                    for stock in stock_list:
                        if self.parent.add_stock_to_monitoring(stock['code'], stock['name']):
                            added_count += 1
                    
                    self.logger.info(f"✅ 조건검색 결과 모니터링 추가 완료: {added_count}개 종목")
                    self.logger.info("📋 조건검색 결과가 모니터링 종목에 추가되었습니다")
            else:
                self.logger.warning("⚠️ 조건검색 결과에 유효한 종목이 없습니다")
            
        except Exception as e:
            self.logger.error(f"❌ 조건검색 일반 요청 응답 처리 실패: {e}")
            import traceback
            self.logger.error(f"조건검색 일반 요청 응답 처리 에러 상세: {traceback.format_exc()}")

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
        
        # API 제한 관리자 초기화
        self.api_limit_manager = ApiRequestManager()
        
        # 모의투자 상태 로그 출력
        if self.is_mock:
            self.logger.info("모의투자 서버 사용 모드로 설정됨")
        else:
            self.logger.info("실거래 서버 사용 모드로 설정됨")
        
        # 인증 토큰
        self.access_token = None
        self.token_expires_at = None
        self.token_file = 'kiwoom_token.json'  # 토큰 저장 파일
        
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
        
        # 웹소켓 관련 속성들은 KiwoomWebSocketClient에서 처리
        # stock_data와 balance_data는 웹소켓에서만 사용됨
        
        # 프로그램 시작 시 저장된 토큰 로드 시도
        self.load_saved_token()
        
    def load_config(self):
        """설정 파일 로드"""
        import configparser
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
                        self.logger.debug(f"토큰 발급 응답: {json.dumps(token_data, indent=2, ensure_ascii=False)}")
                        
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
        """주식현재가 시세 조회 (실시간 주식 정보)"""
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
            
            response = requests.get(url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                self.logger.debug(f"주식현재가 조회 응답: {json.dumps(data, indent=2, ensure_ascii=False)}")
                
                # 응답 코드 확인
                if data.get('return_code') == 0:
                    self.logger.info(f"주식현재가 조회 성공: {code}")
                    return self._parse_stock_price_data(data)
                else:
                    return_msg = data.get('return_msg', '알 수 없는 오류')
                    self.logger.error(f"주식현재가 조회 실패: {return_msg}")
                    return {}
            else:
                self.logger.error(f"주식현재가 조회 실패: {response.status_code}")
                self.logger.error(f"응답: {response.text}")
                return {}
                
        except Exception as e:
            self.logger.error(f"주식현재가 조회 중 오류: {e}")
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
    
    def get_stock_chart_data(self, code: str, period: str = "1m", count: int = 100) -> pd.DataFrame:
        """주식 차트 데이터 조회"""
        try:
            if not self.check_token_validity():
                return pd.DataFrame()
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/chart"
            
            params = {
                "code": code,
                "period": period,
                "count": count
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
    
    def get_stock_tick_chart(self, code: str, tic_scope: int = 30, count: int = 30, cont_yn: str = 'N', next_key: str = '') -> Dict:
        """주식 틱 차트 데이터 조회 (ka10079) - 참고 코드 기반 개선"""
        try:
            if not self.check_token_validity():
                return {}
            
            # API 요청 제한 확인 및 대기
            ApiLimitManager.check_api_limit_and_wait("틱 차트 조회", request_type="tick")
            
            # 모의투자 여부에 따라 서버 선택 (참고 코드와 동일한 방식)
            if self.is_mock:
                host = 'https://mockapi.kiwoom.com'  # 모의투자
            else:
                host = 'https://api.kiwoom.com'      # 실전투자
            
            endpoint = '/api/dostk/chart'
            url = host + endpoint
            
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
                return self._parse_tick_chart_data(response_data, count)
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
    
    def get_stock_minute_chart(self, code: str, period: int = 3, count: int = 20) -> Dict:
        """주식 분봉 차트 데이터 조회 (ka10080)"""
        try:
            if not self.check_token_validity():
                return {}
            
            # API 요청 제한 확인 및 대기
            ApiLimitManager.check_api_limit_and_wait("분봉 차트 조회", request_type="minute")
            
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
                return self._parse_minute_chart_data(response_data, count)
            else:
                self.logger.error(f"분봉 차트 데이터 조회 실패: {response.status_code}")
                return {}
                
        except Exception as e:
            self.logger.error(f"분봉 차트 데이터 조회 중 오류: {e}")
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
            
            self.logger.debug(f"계좌평가현황 요청: {url}")
            self.logger.debug(f"요청 헤더: {headers}")
            self.logger.debug(f"요청 데이터: {json.dumps(params, indent=2, ensure_ascii=False)}")
            self.logger.debug(f"응답 상태 코드: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                self.logger.debug(f"계좌평가현황 응답: {json.dumps(data, indent=2, ensure_ascii=False)}")
                
                # 응답 코드 확인
                if data.get('return_code') == 0:
                    self.logger.info("계좌평가현황 조회 성공")
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
        """매수 주문 (키움 REST API 기반)"""
        try:
            if not self.check_token_validity():
                return False
            
            # 키움 REST API 주식주문(현금) 호출
            result = self.order_cash(
                env_dv="demo" if self.is_mock else "real",
                ord_dv="buy",
                cano=self.account_number,
                acnt_prdt_cd=self.account_product_code,
                pdno=code,
                ord_dvsn=self._get_order_division(order_type),
                ord_qty=str(quantity),
                ord_unpr=str(price) if price > 0 else "",
                excg_id_dvsn_cd="KRX"
            )
            
            if result is not None and not result.empty:
                self.logger.info(f"매수 주문 성공: {code} {quantity}주")
                return True
            else:
                self.logger.error(f"매수 주문 실패: {code}")
                return False
                
        except Exception as e:
            self.logger.error(f"매수 주문 중 오류: {e}")
            return False
    
    def place_sell_order(self, code: str, quantity: int, price: int = 0, order_type: str = "market") -> bool:
        """매도 주문 (키움 REST API 기반)"""
        try:
            if not self.check_token_validity():
                return False
            
            # 키움 REST API 주식주문(현금) 호출
            result = self.order_cash(
                env_dv="demo" if self.is_mock else "real",
                ord_dv="sell",
                cano=self.account_number,
                acnt_prdt_cd=self.account_product_code,
                pdno=code,
                ord_dvsn=self._get_order_division(order_type),
                ord_qty=str(quantity),
                ord_unpr=str(price) if price > 0 else "",
                excg_id_dvsn_cd="KRX"
            )
            
            if result is not None and not result.empty:
                self.logger.info(f"매도 주문 성공: {code} {quantity}주")
                return True
            else:
                self.logger.error(f"매도 주문 실패: {code}")
                return False
                
        except Exception as e:
            self.logger.error(f"매도 주문 중 오류: {e}")
            return False
    
    def order_cash(self, env_dv: str, ord_dv: str, cano: str, acnt_prdt_cd: str, 
                   pdno: str, ord_dvsn: str, ord_qty: str, ord_unpr: str, 
                   excg_id_dvsn_cd: str, sll_type: str = "", cndt_pric: str = ""):
        """주식주문(현금) - 키움 REST API"""
        try:
            if not self.check_token_validity():
                return None
            
            # API URL 설정
            api_url = "/uapi/domestic-stock/v1/trading/order-cash"
            
            # tr_id 설정
            if env_dv == "real":
                if ord_dv == "sell":
                    tr_id = "TTTC0011U"
                elif ord_dv == "buy":
                    tr_id = "TTTC0012U"
                else:
                    raise ValueError("ord_dv can only be sell or buy")
            elif env_dv == "demo":
                if ord_dv == "sell":
                    tr_id = "VTTC0011U"
                elif ord_dv == "buy":
                    tr_id = "VTTC0012U"
                else:
                    raise ValueError("ord_dv can only be sell or buy")
            else:
                raise ValueError("env_dv is required (e.g. 'real' or 'demo')")
            
            # 요청 파라미터
            params = {
                "CANO": cano,  # 종합계좌번호
                "ACNT_PRDT_CD": acnt_prdt_cd,  # 계좌상품코드
                "PDNO": pdno,  # 상품번호
                "ORD_DVSN": ord_dvsn,  # 주문구분
                "ORD_QTY": ord_qty,  # 주문수량
                "ORD_UNPR": ord_unpr,  # 주문단가
                "EXCG_ID_DVSN_CD": excg_id_dvsn_cd,  # 거래소ID구분코드
                "SLL_TYPE": sll_type,  # 매도유형
                "CNDT_PRIC": cndt_pric  # 조건가격
            }
            
            # API 호출
            response = self._make_request(api_url, tr_id, params, post_flag=True)
            
            if response and response.get('rt_cd') == '0':
                self.logger.info(f"주식주문(현금) 성공: {ord_dv} {pdno} {ord_qty}주")
                return response.get('output', {})
            else:
                error_msg = response.get('msg1', 'Unknown error') if response else 'No response'
                self.logger.error(f"주식주문(현금) 실패: {error_msg}")
                return None
                
        except Exception as e:
            self.logger.error(f"주식주문(현금) 중 오류: {e}")
            return None
    
    def order_credit(self, ord_dv: str, cano: str, acnt_prdt_cd: str, pdno: str,
                     crdt_type: str, loan_dt: str, ord_dvsn: str, ord_qty: str, 
                     ord_unpr: str, excg_id_dvsn_cd: str = "KRX", **kwargs):
        """주식주문(신용) - 키움 REST API"""
        try:
            if not self.check_token_validity():
                return None
            
            # API URL 설정
            api_url = "/uapi/domestic-stock/v1/trading/order-credit"
            
            # tr_id 설정
            if ord_dv == "buy":
                tr_id = "TTTC0052U"
            elif ord_dv == "sell":
                tr_id = "TTTC0051U"
            else:
                raise ValueError("ord_dv can only be buy or sell")
            
            # 요청 파라미터
            params = {
                "CANO": cano,
                "ACNT_PRDT_CD": acnt_prdt_cd,
                "PDNO": pdno,
                "CRDT_TYPE": crdt_type,
                "LOAN_DT": loan_dt,
                "ORD_DVSN": ord_dvsn,
                "ORD_QTY": ord_qty,
                "ORD_UNPR": ord_unpr,
                "EXCG_ID_DVSN_CD": excg_id_dvsn_cd
            }
            
            # 추가 파라미터 처리
            for key, value in kwargs.items():
                if value:  # 빈 값이 아닌 경우만 추가
                    params[key.upper()] = value
            
            # API 호출
            response = self._make_request(api_url, tr_id, params, post_flag=True)
            
            if response and response.get('rt_cd') == '0':
                self.logger.info(f"주식주문(신용) 성공: {ord_dv} {pdno} {ord_qty}주")
                return response.get('output', {})
            else:
                error_msg = response.get('msg1', 'Unknown error') if response else 'No response'
                self.logger.error(f"주식주문(신용) 실패: {error_msg}")
                return None
                
        except Exception as e:
            self.logger.error(f"주식주문(신용) 중 오류: {e}")
            return None
    
    def _make_request(self, api_url: str, tr_id: str, params: dict, post_flag: bool = False):
        """키움 REST API 요청"""
        try:
            # 서버 URL 설정
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}{api_url}"
            
            # 헤더 설정
            headers = {
                'Content-Type': 'application/json; charset=utf-8',
                'authorization': f'Bearer {self.access_token}',
                'appkey': self.app_key,
                'appsecret': self.app_secret,
                'tr_id': tr_id
            }
            
            if post_flag:
                # POST 요청
                response = self.session.post(url, headers=headers, json=params)
            else:
                # GET 요청
                response = self.session.get(url, headers=headers, params=params)
            
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"API 요청 실패: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            self.logger.error(f"API 요청 중 오류: {e}")
            return None
    
    def _get_order_division(self, order_type: str) -> str:
        """주문구분 코드 변환"""
        order_divisions = {
            "market": "00",      # 시장가
            "limit": "00",       # 지정가
            "stop": "05",        # 조건부지정가
            "stop_limit": "05"   # 조건부지정가
        }
        return order_divisions.get(order_type, "00")  # 기본값: 지정가
    
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
    
# 웹소켓 관련 메서드 제거됨 - KiwoomWebSocketClient에서 처리

# _run_websocket_client 메서드 제거됨 - KiwoomWebSocketClient에서 처리

# _websocket_main 메서드 제거됨 - KiwoomWebSocketClient에서 처리

# connect_websocket 메서드 제거됨 - stock_trader.py에서 직접 처리
    
# get_websocket_data, get_websocket_balance_data 메서드 제거됨 - KiwoomWebSocketClient에서 처리
    
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
    
    def _parse_tick_chart_data(self, data: Dict, count: int) -> Dict:
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
            
            tick_data = data['stk_tic_chart_qry']
            if not tick_data:
                self.logger.warning("틱 차트 데이터가 비어있습니다")
                return {}
            
            # 필요한 필드 추출
            parsed_data = {
                'time': [],
                'open': [],
                'high': [],
                'low': [],
                'close': [],
                'volume': [],
                'last_tic_cnt': []
            }
            
            # 디버깅: 원본 데이터 시간 순서 확인
            if tick_data:
                original_first = tick_data[0].get('cntr_tm', '')
                original_last = tick_data[-1].get('cntr_tm', '')
                self.logger.debug(f"틱 원본 데이터: 총 {len(tick_data)}개, 첫번째={original_first}, 마지막={original_last}")
            
            # 시간 순서를 정상적으로 정렬 (오래된 시간부터 최신 시간 순서)
            tick_data.sort(key=lambda x: x.get('cntr_tm', ''))
            
            # 최신 count개만 가져오기 (정렬 후 슬라이스)
            data_to_process = tick_data[-count:] if len(tick_data) > count else tick_data
            
            # 디버깅: 시간 순서 확인
            if data_to_process:
                first_time = data_to_process[0].get('cntr_tm', '')
                last_time = data_to_process[-1].get('cntr_tm', '')
                self.logger.debug(f"틱 데이터 시간 순서 (정렬 후): 총 {len(data_to_process)}개, 첫번째={first_time}, 마지막={last_time}")
            
            for item in data_to_process:
                # 시간 정보 (cntr_tm 필드 사용 - 체결시간)
                time_str = item.get('cntr_tm', '')
                if time_str:
                    try:
                        from datetime import datetime
                        # 체결시간 형식에 따라 파싱 (HHMMSS 또는 YYYYMMDDHHMMSS)
                        if len(time_str) == 6:  # HHMMSS
                            # 현재 날짜와 결합
                            today = datetime.now().strftime('%Y%m%d')
                            full_time = f"{today}{time_str}"
                            dt = datetime.strptime(full_time, '%Y%m%d%H%M%S')
                        elif len(time_str) == 14:  # YYYYMMDDHHMMSS
                            dt = datetime.strptime(time_str, '%Y%m%d%H%M%S')
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
                
                # 안전한 숫자 변환 함수
                def safe_float(value, default=0.0):
                    if value == '' or value is None:
                        return default
                    try:
                        # 문자열에서 숫자만 추출 (음수 부호, 소수점 포함)
                        if isinstance(value, str):
                            # 공백 제거
                            value = value.strip()
                            # 빈 문자열 체크
                            if not value:
                                return default
                        return float(value)
                    except (ValueError, TypeError):
                        self.logger.warning(f"가격 데이터 변환 실패: '{value}' -> 기본값 {default} 사용")
                        return default
                
                open_price = abs(safe_float(raw_open))
                high_price = abs(safe_float(raw_high))
                low_price = abs(safe_float(raw_low))
                close_price = abs(safe_float(raw_close))
                volume = int(safe_float(raw_volume, 0))
                
                # OHLC 논리 검증
                if not (low_price <= min(open_price, close_price) and max(open_price, close_price) <= high_price):
                    self.logger.warning(f"틱 OHLC 논리 오류: O={open_price}, H={high_price}, L={low_price}, C={close_price}")
                
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
                
                # 마지막틱갯수 (last_tic_cnt) 필드 추가
                last_tic_cnt = item.get('last_tic_cnt', '')
                parsed_data['last_tic_cnt'].append(last_tic_cnt)
            
            self.logger.debug(f"틱 차트 데이터 파싱 완료: {len(parsed_data['close'])}개 데이터")
            return parsed_data
            
        except Exception as e:
            self.logger.error(f"틱 차트 데이터 파싱 오류: {e}")
            return {}
    
    def _parse_minute_chart_data(self, data: Dict, count: int) -> Dict:
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
            
            # 최신 count개만 가져오기 (정렬 후 슬라이스)
            data_to_process = minute_data[-count:] if len(minute_data) > count else minute_data
            
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
                        from datetime import datetime
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
                
                # 안전한 숫자 변환 함수
                def safe_float(value, default=0.0):
                    if value == '' or value is None:
                        return default
                    try:
                        # 문자열에서 숫자만 추출 (음수 부호, 소수점 포함)
                        if isinstance(value, str):
                            # 공백 제거
                            value = value.strip()
                            # 빈 문자열 체크
                            if not value:
                                return default
                        return float(value)
                    except (ValueError, TypeError):
                        self.logger.warning(f"가격 데이터 변환 실패: '{value}' -> 기본값 {default} 사용")
                        return default
                
                open_price = abs(safe_float(raw_open))
                high_price = abs(safe_float(raw_high))
                low_price = abs(safe_float(raw_low))
                close_price = abs(safe_float(raw_close))
                volume = int(safe_float(raw_volume, 0))
                
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
    
    def get_market_status(self) -> Dict:
        """시장 상태 조회"""
        try:
            if not self.check_token_validity():
                return {}
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/mrkcond"
            
            response = self.session.get(url)
            
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"시장 상태 조회 실패: {response.status_code}")
                return {}
                
        except Exception as e:
            self.logger.error(f"시장 상태 조회 중 오류: {e}")
            return {}
    
    def is_market_open(self) -> bool:
        """시장 개장 여부 확인"""
        try:
            market_status = self.get_market_status()
            return market_status.get('is_open', False)
        except Exception as e:
            self.logger.error(f"시장 개장 확인 중 오류: {e}")
            return False
    
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


"""
키움 REST API 클라이언트
크레온 플러스 API를 키움 REST API로 대체하는 클라이언트 클래스
"""
import requests
import json
import time
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import pandas as pd
import sqlite3
from threading import Lock
import queue
import asyncio
import websockets
import threading


if __name__ == "__main__":
    qasync.run(main())
