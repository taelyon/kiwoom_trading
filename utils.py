import logging
import os
import sys
import time
import ctypes
import asyncio
import threading
import sqlite3
from datetime import datetime

# PyQt6 제거됨 (Headless CLI 최적화)

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

# LogSignaler 및 QTextEditLogger 제거됨 (Headless CLI 최적화)

def setup_logging():
    """로그 설정"""
    import sys
    try:
        # Docker 컨테이너 등에서 콘솔 로그가 지연 출력(버퍼링)되는 현상 방지
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

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
        
        # root 로거 설정 (전체적으로는 DEBUG 레벨까지 일단 허용)
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        
        # 기존 핸들러 제거
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # 1. 메인 파일 핸들러 (kiwoom_trader.log: INFO 레벨 이상, 일별 롤링 7일 보관)
        from logging.handlers import TimedRotatingFileHandler
        file_handler = TimedRotatingFileHandler(
            log_filename, when='midnight', interval=1, backupCount=7, encoding='utf-8'
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        
        # 2. 디버그 전용 파일 핸들러 (kiwoom_debug.log: DEBUG 레벨 전체, 일별 롤링 3일 보관)
        debug_filename = f"{log_dir}/kiwoom_debug.log"
        debug_file_handler = TimedRotatingFileHandler(
            debug_filename, when='midnight', interval=1, backupCount=3, encoding='utf-8'
        )
        debug_file_handler.setLevel(logging.DEBUG)
        debug_file_handler.setFormatter(formatter)
        root_logger.addHandler(debug_file_handler)

        # 3. 매매 전용 전용 로거 & 파일 핸들러 (trades.log: 매매 관련 핵심 이벤트만 기록, 30일 보관)
        trades_filename = f"{log_dir}/trades.log"
        trades_file_handler = TimedRotatingFileHandler(
            trades_filename, when='midnight', interval=1, backupCount=30, encoding='utf-8'
        )
        trades_file_handler.setLevel(logging.INFO)
        trades_file_handler.setFormatter(formatter)
        
        trade_logger = logging.getLogger("trades")
        trade_logger.setLevel(logging.INFO)
        trade_logger.addHandler(trades_file_handler)
        trade_logger.propagate = True  # root 및 대시보드 로거에도 전달
        
        # 콘솔/터미널 핸들러
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
        
        # 실시간 웹 대시보드 로깅 가로채기를 위한 커스텀 핸들러 설정 (극초기 등록)
        try:
            from web_dashboard import WebDashboardLogHandler
            dashboard_handler = WebDashboardLogHandler()
            root_logger.addHandler(dashboard_handler)
        except Exception:
            pass
        
        # aiosqlite DEBUG 로그 비활성화
        aiosqlite_logger = logging.getLogger('aiosqlite')
        aiosqlite_logger.setLevel(logging.WARNING)
        
        # qasync DEBUG 로그 비활성화
        qasync_logger = logging.getLogger('qasync')
        qasync_logger.setLevel(logging.WARNING)
        
        # websockets 라이브러리의 노이즈성 연결/핸드셰이크 에러 로그 비활성화
        logging.getLogger('websockets').setLevel(logging.CRITICAL)
        logging.getLogger('websockets.client').setLevel(logging.CRITICAL)
        logging.getLogger('websockets.server').setLevel(logging.CRITICAL)
        
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

        # QTextEditLogger 제거됨 (Headless CLI 최적화)
                
    except Exception as ex:
        print(f"로깅 설정 실패: {ex}")

class ApiLimitManager:
    """API 제한 관리 클래스 (개선된 버전)"""
    logger = logging.getLogger(__qualname__)
    
    # 스레드 동기화용 락
    _lock = threading.Lock()
    
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
            
            # 차트 요청들은 동일한 큐를 공유하여 동시 다발적인 요청 방지 (429 에러 방지)
            queue_key = request_type
            if request_type in ['tic_chart', 'minute_chart', 'tic', 'minute']:
                queue_key = 'chart_req'
                interval = 1.5  # 차트 요청 간격 1.5초로 설정하여 429 예방 (안전 마진 대폭 확보)
            
            # 임계 영역 보호 - 스레드 락 적용으로 레이스 컨디션 차단
            with cls._lock:
                current_time = time.time()
                # 이 요청 타입의 다음 사용 가능 시간 확인
                next_time = cls._next_request_time.get(queue_key, current_time)
                
                # 다음 사용 가능 시간이 과거라면 현재 시간으로 갱신
                if next_time < current_time:
                    next_time = current_time
                    
                # 대기 시간 계산
                wait_time = next_time - current_time
                
                # 다음 요청을 위해 예약 시간 슬롯을 확보 (Interval 추가)
                cls._next_request_time[queue_key] = next_time + interval
            
            # 락을 해제한 상태에서 비동기 대기
            if wait_time > 0:
                if wait_time > 1.0:
                    cls.logger.warning(f"⏳ [API제한] {operation_name} 요청이 {wait_time:.1f}초 대기 중 (큐: {queue_key}, 간격: {interval}초)")
                await asyncio.sleep(wait_time)
            
            return True
            
        except Exception as ex:
            cls.logger.error(f"API 제한 확인 중 오류: {ex}")
            return False

class CallbackSignal:
    """PyQt6 pyqtSignal을 대체하기 위한 콜백 기반 시그널 클래스 (공통 유틸)"""
    def __init__(self):
        self.callbacks = []
        
    def connect(self, callback):
        if callback not in self.callbacks:
            self.callbacks.append(callback)
        
    def disconnect(self, callback=None):
        if callback is None:
            self.callbacks.clear()
        elif callback in self.callbacks:
            self.callbacks.remove(callback)
            
    def emit(self, *args, **kwargs):
        for callback in self.callbacks:
            try:
                callback(*args, **kwargs)
            except Exception:
                pass

