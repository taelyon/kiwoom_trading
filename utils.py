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

IS_WINDOWS = sys.platform.startswith('win')

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
