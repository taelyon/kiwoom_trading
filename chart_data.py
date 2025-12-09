import logging
import asyncio
from collections import deque
import time
import concurrent.futures
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import talib
from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from utils import ApiLimitManager
import strategy_utils

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
            self.realtime_tick_times = {} # {code: deque(maxlen=10)} - 틱 생성 속도 계산용
            self.pending_stocks = {}  # 큐에 대기 중인 종목 정보 (코드: 이름)
            self.logger.debug("🔍 API 요청 큐 시스템 초기화 완료")
            
            # QTimer 객체를 즉시 생성
            self.update_timer = QTimer(self)
            self.update_timer.timeout.connect(self.update_all_charts)
            
            self.save_timer = QTimer(self)
            self.save_timer.timeout.connect(self._trigger_async_save_to_database)
            
            self.queue_timer = QTimer(self)
            self.logger.debug("🔍 타이머 객체 즉시 생성 완료")
            
            self.logger.debug("📊 차트 데이터 캐시 초기화 완료")
        except Exception as ex:
            self.logger.error(f"❌ ChartDataCache 초기화 실패: {ex}", exc_info=True)
            raise ex

    def stop(self):
        """모든 타이머 중지 및 리소스 정리"""
        try:
            if self.update_timer.isActive():
                self.update_timer.stop()
            if self.save_timer.isActive():
                self.save_timer.stop()
            if self.queue_timer.isActive():
                self.queue_timer.stop()
            self.logger.info("⏹️ ChartDataCache 타이머 중지 완료")
        except Exception as ex:
            self.logger.error(f"❌ ChartDataCache 중지 실패: {ex}")
    

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
        if not self.queue_processing:
            self.logger.debug(f"데이터 수집 건너뜀 (큐 처리 중 아님): {code}")
            return
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
        except asyncio.CancelledError:
            self.logger.debug(f"데이터 수집 작업 취소됨: {code}")
            # 작업이 취소되었을 때는 오류 로그를 남기지 않고 조용히 종료
            
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
                
                self.cache[code] = {
                    'tic_data': None,
                    'min_data': None,
                    'last_update': None,
                    'last_save': None,
                    'previous_close': 0,
                    'current_open': 0
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
            
            self.logger.debug(f"📋 주기적 업데이트: {added_count}개 종목을 API 요청 큐에 추가 (총 큐: {len(self.api_request_queue)}개)")
            
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

    def remove_stock(self, code):
        """캐시에서 종목 제거 및 데이터 수집 중단"""
        try:
            # 캐시에서 제거
            if code in self.cache:
                del self.cache[code]
                self.logger.debug(f"🗑️ ChartDataCache: {code} 캐시 데이터 제거됨")
            
            # API 큐에서 제거
            if code in self.api_request_queue:
                self.api_request_queue.remove(code)
                self.logger.debug(f"🗑️ ChartDataCache: {code} API 큐에서 제거됨")
            
            # 활성 태스크 취소
            if code in self.active_chart_tasks:
                task = self.active_chart_tasks[code]
                if not task.done():
                    task.cancel()
                    self.logger.debug(f"🗑️ ChartDataCache: {code} 데이터 수집 태스크 취소됨")
                del self.active_chart_tasks[code]
                
            return True
        except Exception as ex:
            self.logger.error(f"ChartDataCache 종목 제거 실패 ({code}): {ex}", exc_info=True)
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
                
            # 장 마감 시간(15:30) 이후 체크 로직 제거 - 자동 청산 완료 시 외부에서 stop() 호출로 제어됨
            # market_close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
            # if now > market_close_time:
            #     logging.debug(f"⏰ 장 마감 시간({market_close_time.strftime('%H:%M:%S')}) 이후이므로 DB 저장을 중지합니다.")
            #     return

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
                    
                    # 3분봉 데이터는 MA, RSI, MACD만 계산
                    min_allowed = ['MA5', 'MA10', 'MA20', 'MA50', 'MA60', 'MA120', 'RSI', 'MACD', 'MACD_SIGNAL', 'MACD_HIST']
                    min_indicators = strategy_utils.KiwoomIndicatorExtractor.extract_chart_indicators(min_df, allowed_indicators=min_allowed)

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
                logging.debug(f"ℹ️ 저장된 차트 데이터(틱/분봉)가 없습니다 (모니터링 종목: {len(self.cache)}개)")
                
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
            # update_timer는 모니터링 종목 수에 따라 동적으로 간격이 조절되므로 여기서 시작하지 않음
            self.update_chart_update_interval()

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

    def update_chart_update_interval(self):
        """모니터링 종목 수에 따라 차트 업데이트 주기를 동적으로 조절합니다."""
        try:
            if not self.update_timer:
                return

            monitoring_codes = self.parent.monitoring_manager.get_monitoring_stock_codes()
            num_stocks = len(monitoring_codes)

            if num_stocks > 0:
                # 종목당 3초 간격으로 설정
                new_interval_seconds = num_stocks * 3
                new_interval_ms = new_interval_seconds * 1000

                # 현재 타이머 간격과 다를 경우에만 업데이트
                if self.update_timer.interval() != new_interval_ms or not self.update_timer.isActive():
                    self.update_timer.setInterval(new_interval_ms)
                    if not self.update_timer.isActive(): self.update_timer.start()
                    self.logger.debug(f"🔄 차트 업데이트 주기 변경: {num_stocks}개 종목 * 3초 = {new_interval_seconds}초")
            else:
                # 모니터링 종목이 없으면 타이머 중지
                if self.update_timer.isActive():
                    self.update_timer.stop()
                    self.logger.debug("⏹️ 모니터링 종목이 없어 차트 업데이트 타이머를 중지합니다.")
        except Exception as ex:
            self.logger.error(f"❌ 차트 업데이트 주기 조절 실패: {ex}", exc_info=True)
    
    def _calculate_technical_indicators(self, data, chart_type=None):
        """기술적 지표 계산"""
        try:
            if not data or not isinstance(data, dict):
                return data
                
            close_prices = data.get('close', [])
            high_prices = data.get('high', [])
            low_prices = data.get('low', [])
            volumes = data.get('volume', [])
            
            # 데이터 길이 일치화 (가장 짧은 길이로 맞춤)
            min_len = min(len(close_prices), len(high_prices), len(low_prices), len(volumes))
            
            if min_len < 5:
                return data
                
            # 모든 배열을 최소 길이에 맞춰 자름 (데이터 무결성 보장)
            close_prices = close_prices[:min_len]
            high_prices = high_prices[:min_len]
            low_prices = low_prices[:min_len]
            volumes = volumes[:min_len]
            
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
            if chart_type != "minute" or True: # RSI는 3분봉에도 포함
                if len(close_array) >= 14:
                    indicators['RSI'] = talib.RSI(close_array, timeperiod=14)
                
            # MACD 계산
            if chart_type != "minute" or True: # MACD는 3분봉에도 포함
                if len(close_array) >= 26:
                    macd, macd_signal, macd_hist = talib.MACD(close_array)
                    indicators['MACD'] = macd
                    indicators['MACD_SIGNAL'] = macd_signal
                    indicators['MACD_HIST'] = macd_hist
                
            # 볼린저 밴드 (3분봉 제외)
            if chart_type != "minute":
                if len(close_array) >= 20:
                    bb_upper, bb_middle, bb_lower = talib.BBANDS(close_array, timeperiod=20)
                    indicators['BB_UPPER'] = bb_upper
                    indicators['BB_MIDDLE'] = bb_middle
                    indicators['BB_LOWER'] = bb_lower
                
            # 스토캐스틱 (3분봉 제외)
            if chart_type != "minute":
                if len(high_array) >= 14 and len(low_array) >= 14:
                    slowk, slowd = talib.STOCH(high_array, low_array, close_array)
                    indicators['STOCH_K'] = slowk
                    indicators['STOCH_D'] = slowd
                
            # Williams %R (3분봉 제외)
            if chart_type != "minute":
                if len(high_array) >= 14 and len(low_array) >= 14:
                    williams_r = talib.WILLR(high_array, low_array, close_array, timeperiod=14)
                    indicators['WILLIAMS_R'] = williams_r
                
            # ROC (Rate of Change) (3분봉 제외)
            if chart_type != "minute":
                if len(close_array) >= 10:
                    roc = talib.ROC(close_array, timeperiod=10)
                    indicators['ROC'] = roc
                
            # OBV (On Balance Volume) (3분봉 제외)
            if chart_type != "minute":
                if len(close_array) >= 1 and len(volume_array) >= 1:
                    obv = talib.OBV(close_array, volume_array)
                    indicators['OBV'] = obv
                    
                    # OBV의 20일 이동평균
                    if len(obv) >= 20:
                        obv_ma20 = talib.SMA(obv, timeperiod=20)
                        indicators['OBV_MA20'] = obv_ma20
                
            # ATR (Average True Range) (3분봉 제외)
            if chart_type != "minute":
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

            # 틱 생성 속도(Tick Velocity) 계산
            # 정의: 직전 10개의 틱이 체결되는 데 걸린 시간 (밀리초 단위)
            current_ms = time.time() * 1000
            if stock_code not in self.realtime_tick_times:
                self.realtime_tick_times[stock_code] = deque(maxlen=10)
            
            self.realtime_tick_times[stock_code].append(current_ms)
            
            tick_velocity = 999999.0 # 기본값 (데이터 부족 시 매우 느림으로 간주)
            if len(self.realtime_tick_times[stock_code]) >= 10:
                # 가장 최근 시간 - 10번째 전 시간
                # deque는 [oldest, ..., newest] 순서
                time_diff = self.realtime_tick_times[stock_code][-1] - self.realtime_tick_times[stock_code][0]
                tick_velocity = float(time_diff)
            
            # 실시간 메트릭 저장
            if 'realtime_metrics' not in cached_data:
                cached_data['realtime_metrics'] = {}
            cached_data['realtime_metrics']['tick_velocity'] = tick_velocity
            
            # 차트 캐시 업데이트 (메트릭 포함)
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

    async def add_realtime_order_book_data_async(self, stock_code, order_book_data):
        """실시간 호가 데이터를 처리하고 메트릭을 업데이트"""
        try:
            # MyWindow의 chart_cache에 접근
            if not hasattr(self, 'parent') or not self.parent:
                return

            chart_cache = self.parent.chart_cache
            
            # 차트 캐시에서 기존 데이터 가져오기
            cached_data = chart_cache.get_cached_data(stock_code)
            
            if not cached_data or not isinstance(cached_data, dict):
                # 호가 데이터만으로는 차트를 생성하지 않으므로 캐시가 없으면 스킵
                return
                
            total_sell_hoga = order_book_data.get('total_sell_hoga', 0)
            total_buy_hoga = order_book_data.get('total_buy_hoga', 0)
            
            # 호가 불균형 계산 (Order Book Imbalance)
            # (매수 잔량 - 매도 잔량) / (매수 잔량 + 매도 잔량)
            if total_buy_hoga + total_sell_hoga > 0:
                imbalance = (total_buy_hoga - total_sell_hoga) / (total_buy_hoga + total_sell_hoga)
            else:
                imbalance = 0.0
                
            # 실시간 메트릭 저장
            if 'realtime_metrics' not in cached_data:
                cached_data['realtime_metrics'] = {}
            
            cached_data['realtime_metrics']['order_book_imbalance'] = imbalance
            cached_data['realtime_metrics']['total_sell_hoga'] = total_sell_hoga
            cached_data['realtime_metrics']['total_buy_hoga'] = total_buy_hoga
            
            # 차트 캐시 업데이트
            chart_cache.cache[stock_code] = cached_data
            
             # 데이터 업데이트 시그널 발생 (필요 시)
            # self.data_updated.emit(stock_code) # 호가 변경만으로 차트를 다시 그릴 필요가 없다면 주석 처리
            
        except Exception as e:
            self.logger.error(f"실시간 호가 데이터 처리 실패: {e}", exc_info=True)


