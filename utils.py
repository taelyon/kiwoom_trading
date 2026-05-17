import logging
import os
import sys
import time
import ctypes
import asyncio
import sqlite3
from datetime import datetime

# PyQt6 관련
from PyQt6.QtWidgets import QTextEdit
from PyQt6.QtCore import QObject, pyqtSignal

IS_WINDOWS = sys.platform.startswith('win')

# 글로벌 비동기 Task GC 소멸 방지용 Tracker
_global_background_tasks = set()

def create_fire_and_forget_task(coro):
    """
    Task GC 소멸을 방지하는 안전한 fire-and-forget 비동기 작업 생성
    """
    try:
        task = asyncio.create_task(coro)
        _global_background_tasks.add(task)
        task.add_done_callback(_global_background_tasks.discard)
        return task
    except RuntimeError:
        logging.getLogger('utils').warning("⚠️ 이벤트 루프가 없어 비동기 작업을 실행할 수 없습니다")
        return None

def adapt_datetime_iso(val):
    """datetime을 ISO 형식 문자열로 변환"""
    return val.isoformat()

def convert_datetime(val):
    """ISO 형식 문자열을 datetime으로 변환"""
    return datetime.fromisoformat(val.decode())

# sqlite3 datetime adapter 등록
sqlite3.register_adapter(datetime, adapt_datetime_iso)
sqlite3.register_converter("datetime", convert_datetime)

def _prevent_system_sleep():
    """Windows 환경에서만 동작하는 절전 모드 해제 처리"""
    if not IS_WINDOWS or not hasattr(ctypes, "windll"):
        return

    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception as ex:
        logging.warning(f"시스템 절전 방지 설정 실패: {ex}")

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

class LogSignaler(QObject):
    """로그 메시지 전달을 위한 시그널러"""
    log_signal = pyqtSignal(str)

class QTextEditLogger(logging.Handler):
    """QTextEdit에 로그를 출력하는 핸들러 (스레드 안전 - 시그널 사용)"""
    
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget
        self.signaler = LogSignaler()
        self.signaler.log_signal.connect(self.append_log)
        
    def append_log(self, msg):
        """메인 스레드에서 실행되는 로그 추가 메서드"""
        try:
            if not self.text_widget:
                return
                
            # 위젯 유효성 검사 (삭제된 객체 접근 방지)
            try:
                # isVisible 호출로 C++ 객체 존재 여부 간접 확인
                if not hasattr(self.text_widget, 'isVisible'):
                    return
                self.text_widget.isVisible()
            except RuntimeError:
                return

            if hasattr(self.text_widget, 'append'):
                self.text_widget.append(msg)
                
                # 스크롤 처리
                try:
                    if hasattr(self.text_widget, 'verticalScrollBar'):
                        scrollbar = self.text_widget.verticalScrollBar()
                        if scrollbar and scrollbar.isVisible():
                            scrollbar.setValue(scrollbar.maximum())
                except Exception:
                    pass
        except Exception:
            pass
            
    def emit(self, record):
        try:
            msg = self.format(record)
            # 시그널 발생 (스레드 안전)
            self.signaler.log_signal.emit(msg)
        except Exception:
            pass

def setup_logging():
    """로그 설정"""
    try:
        # 로그 디렉토리 생성
        log_dir = "logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        # 로그 파일명 설정 (TimedRotatingFileHandler가 날짜를 알아서 붙임)
        log_filename = f"{log_dir}/kiwoom_trader.log"
        
        # 로그 포맷
        log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        formatter = logging.Formatter(log_format)
        
        # 환경변수에 따른 로그 레벨 설정 (운영 환경은 INFO, 로컬 개발은 DEBUG)
        is_docker = os.environ.get('RUNTIME_ENV') == 'docker'
        log_level = logging.INFO if is_docker else logging.DEBUG
        
        # root 로거 설정
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        
        # 기존 핸들러 제거
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # 파일 핸들러 (최근 7일치만 보관)
        from logging.handlers import TimedRotatingFileHandler
        file_handler = TimedRotatingFileHandler(
            log_filename, when='midnight', interval=1, backupCount=7, encoding='utf-8'
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        
        # 콘솔/터미널 핸들러
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
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

        # httpcore.connection DEBUG 로그 비활성화
        httpcore_conn_logger = logging.getLogger('httpcore.connection')
        httpcore_conn_logger.setLevel(logging.WARNING)

        # UI 로그 핸들러 추가 (INFO 레벨)
        # MyWindow 인스턴스가 생성된 후에 호출되어야 함
        # 주의: setup_logging은 MyWindow 생성 전에 호출될 수 있으므로,
        # MyWindow 생성 후 별도로 핸들러를 추가하는 로직이 필요할 수 있음.
        # 여기서는 일단 생략하고 main에서 처리하거나, global 변수 접근을 조심해야 함.
        # 원본 코드에서는 globals()['main_window']를 참조했음.
        if 'main_window' in sys.modules.get('__main__', {}).__dict__ and sys.modules['__main__'].main_window:
             # UI 로그창 전용 포맷터
            ui_log_format = '%(asctime)s - %(message)s'
            ui_formatter = logging.Formatter(ui_log_format, datefmt='%H:%M:%S')
            text_edit_logger = QTextEditLogger(sys.modules['__main__'].main_window.trading_tab.terminalOutput)
            text_edit_logger.setLevel(logging.INFO)
            text_edit_logger.setFormatter(ui_formatter)
            root_logger.addHandler(text_edit_logger)
                
    except Exception as ex:
        print(f"로깅 설정 실패: {ex}")

class ApiLimitManager:
    """API 제한 관리 클래스 (개선된 버전)"""
    logger = logging.getLogger(__qualname__)
    
    # API 요청을 위한 다음 예약 시간 (Race condition 방지)
    _next_request_time = {}
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
        """API 제한 확인 및 대기 (개선된 병목 제어 버전)"""
        try:
            # 요청 타입별 간격 설정
            if request_type is None:
                request_type = cls._get_request_type(operation_name)
            interval = cls._request_intervals.get(request_type, cls._request_intervals['default'])
            
            current_time = time.time()
            # 이 요청 타입의 다음 사용 가능 시간 확인
            next_time = cls._next_request_time.get(request_type, current_time)
            
            # 다음 사용 가능 시간이 과거라면 현재 시간으로 갱신
            if next_time < current_time:
                next_time = current_time
                
            # 대기 시간 계산
            wait_time = next_time - current_time
            
            # 다음 요청을 위해 예약 시간 슬롯을 확보 (Interval 추가)
            cls._next_request_time[request_type] = next_time + interval
            
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            
            return True
            
        except Exception as ex:
            cls.logger.error(f"API 제한 확인 중 오류: {ex}")
            return False
