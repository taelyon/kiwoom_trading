import logging
import asyncio
from collections import deque
import time
import concurrent.futures
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import talib
from utils import ApiLimitManager, create_fire_and_forget_task, CallbackSignal
import strategy_utils

class ChartDataCache:
    """모니터링 종목 차트 데이터 메모리 캐시 클래지 (Pure Python)"""
    
    def __init__(self, trader, parent):
        try:
            self.logger = logging.getLogger(self.__class__.__name__)
            self.trader = trader            
            self.parent = parent  # TradingApp 객체 저장
            self.cache = {}  # {종목코드: {'tic_data': {}, 'min_data': {}, 'last_update': datetime}}
            self.api_request_count = 0  # API 요청 카운터
            self.last_api_request_time = 0  # 마지막 API 요청 시간
            
            # 시그널 정의 (콜백 기반)
            self.data_updated = CallbackSignal()
            self.cache_cleared = CallbackSignal()
            
            # API 요청 큐 시스템
            self.api_request_queue = []  # API 요청 큐
            self.queue_processing = False  # 큐 처리 중 플래그
            self.active_chart_tasks = {} # 활성 차트 데이터 수집 asyncio 태스크 관리
            self.realtime_tick_times = {} # {code: deque(maxlen=10)} - 틱 생성 속도 계산용
            self.last_indicator_calc_time = {} # {code: float} - 실시간 지표 계산 스로틀링용 (CPU 과부하 방지)
            self.pending_stocks = {}  # 큐에 대기 중인 종목 정보 (코드: 이름)
            self.monitoring_highest_prices = {} # {code: price} - 감시 종목 최고가 추적 (10% 하락 이탈용)
            self.logger.debug("🔍 API 요청 큐 시스템 초기화 완료")
            
            # asyncio 백그라운드 태스크 정의 (QTimer 대체)
            self.update_task = create_fire_and_forget_task(self._update_loop())
            self.save_task = create_fire_and_forget_task(self._save_loop())
            self.queue_task = create_fire_and_forget_task(self._queue_loop())
            self.logger.debug("🔍 백그라운드 비동기 루프 태스크 시작 완료")
            
            self.logger.debug("📊 차트 데이터 캐시 초기화 완료")
        except Exception as ex:
            self.logger.error(f"❌ ChartDataCache 초기화 실패: {ex}", exc_info=True)
            raise ex

    async def _update_loop(self):
        """차트 데이터 주기적 업데이트 루프 (분산 처리 방식으로 API 제한 우회 및 데이터 완전성 확보)"""
        while True:
            try:
                # 차트 업데이트 주기는 chartdata_update_interval 사용 (기본 300초 / 5분)
                chartdata_update_interval = getattr(self.trader, 'chartdata_update_interval', 300)
                
                # 장 시간 체크
                now = datetime.now()
                market_open_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
                market_close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
                
                if now < market_open_time or now > market_close_time:
                    await asyncio.sleep(60) # 장외 시간은 1분마다 체크
                    continue
                
                # 모니터링 종목 가져오기
                monitoring_codes = []
                if self.parent and hasattr(self.parent, 'monitoring_manager'):
                    monitoring_codes = self.parent.monitoring_manager.get_monitoring_stock_codes()
                
                if not monitoring_codes:
                    await asyncio.sleep(5) # 종목이 없으면 5초 후 다시 체크
                    continue
                
                # [중요] 캐시와 모니터링 리스트 동기화 (좀비 데이터 제거)
                self.update_monitoring_stocks(monitoring_codes)
                
                # 부하 분산을 위해 천천히 하나씩 큐에 추가
                # 키움 API 1시간 1000건 제한 방지를 위해 최소 간격을 15초로 강제 (시간당 최대 480 TR로 억제)
                base_interval = chartdata_update_interval / max(1, len(monitoring_codes))
                interval = max(15.0, base_interval)
                
                for code in monitoring_codes:
                    # 현재 장외 시간이 되었는지 도중 체크
                    now = datetime.now()
                    if now < market_open_time or now > market_close_time:
                        break
                        
                    if code not in self.api_request_queue:
                        # 데이터 완전성 확보를 위해 무조건 주기적으로 재수집
                        self.api_request_queue.append(code)
                        self.logger.debug(f"📋 주기적 백그라운드 분산 업데이트: {code} 차트 완전성 확보용 API 큐 추가")
                        
                    await asyncio.sleep(interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ 차트 업데이트 루프 오류: {e}")
                await asyncio.sleep(5)

    async def _save_loop(self):
        """DB 저장 주기적 루프 (기존 save_timer 대체)"""
        while True:
            try:
                await asyncio.sleep(60)  # 60초 간격
                await self.save_to_database()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ DB 저장 루프 오류: {e}")

    async def _queue_loop(self):
        """API 요청 큐 처리 루프 (기존 1초에서 0.2초 주기로 단축)"""
        while True:
            try:
                await asyncio.sleep(0.2)  # 0.2초 간격
                self._process_api_queue()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"❌ API 큐 루프 오류: {e}")

    def stop(self):
        """모든 비동기 태스크 중지 및 리소스 정리"""
        try:
            if hasattr(self, 'update_task') and self.update_task:
                self.update_task.cancel()
            if hasattr(self, 'save_task') and self.save_task:
                self.save_task.cancel()
            if hasattr(self, 'queue_task') and self.queue_task:
                self.queue_task.cancel()
            self.logger.debug("⏹️ ChartDataCache 백그라운드 루프 중지 완료")
        except Exception as ex:
            self.logger.error(f"❌ ChartDataCache 중지 실패: {ex}")
    

    def collect_chart_data_async(self, code, max_retries=3, force=False):
        """비동기 차트 데이터 수집 (asyncio 기반, qasync 통합)"""
        try:
            # qasync 환경에서 메인 이벤트 루프 사용 시도
            try:
                loop = asyncio.get_running_loop()
                # 이미 실행 중인 이벤트 루프가 있으면 태스크로 실행
                task = asyncio.create_task(self._collect_chart_data_internal(code, max_retries, force))
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
                        return loop.run_until_complete(self._collect_chart_data_internal(code, max_retries, force))
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
    
    async def _collect_chart_data_internal(self, code, max_retries=3, force=False):
        """내부 차트 데이터 수집 (asyncio 기반)"""
        # 수동 매매 작업이 진행 중이면 데이터 수집을 건너뜁니다.
        if hasattr(self.parent, 'trading_lock') and self.parent.trading_lock.locked():
            self.logger.debug(f"수동 매매 작업 진행 중 - 차트 데이터 수집 건너뜀: {code}")
            return

        try:
            self.logger.debug(f"📊 차트 데이터 수집 시작: {code}")
            
            # 비동기로 틱 데이터와 분봉 데이터 수집 (순차적으로 수집하여 API 429 에러 완전 방지)
            try:
                tic_data = await self._collect_tic_data_async(code, max_retries)
            except Exception as e:
                tic_data = e
                
            # API 보호를 위해 잠시 대기
            await asyncio.sleep(0.5)
            
            try:
                min_data = await self._collect_minute_data_async(code, max_retries)
            except Exception as e:
                min_data = e
            
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
            
            # 콜백 즉시 호출 (CLI 환경)
            self._on_chart_data_ready(code, tic_data, min_data)
        except asyncio.CancelledError:
            self.logger.debug(f"데이터 수집 작업 취소됨: {code}")
            
        except Exception as e:
            self.logger.error(f"차트 데이터 수집 실패 ({code}): {e}")
            self._on_chart_data_error(code, str(e))
        finally:
            # 태스크 완료 처리
            if code in self.active_chart_tasks:
                self.active_chart_tasks.pop(code)
            self.logger.debug(f"✅ 차트 데이터 수집 태스크 정리 완료: {code}")
    
    async def _collect_tic_data_async(self, code, max_retries=3):
        """틱 데이터 수집 (asyncio 기반)"""
        for attempt in range(max_retries):
            try:
                # 비동기 API 직접 호출
                # (API 제한 확인은 kiwoom_rest.py 내에서 처리됨)
                data = await self.trader.client.get_stock_tic_chart(code, tic_scope=30)
                
                if data:
                    return self._aggregate_30_to_60_ticks(data)
                else:
                    self.logger.warning(f"⚠️ [API 지연 디버깅] 틱 차트 빈 데이터 응답! (시도 {attempt + 1}/{max_retries}) - 키움 서버가 데이터를 주지 않았습니다. 2초 후 재시도합니다.")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2)
                    
            except Exception as e:
                self.logger.warning(f"⚠️ [API 지연 디버깅] 틱 데이터 수집 시도 {attempt + 1}/{max_retries} 실패: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
        
        return None
    
    async def _collect_minute_data_async(self, code, max_retries=3):
        """분봉 데이터 수집 (asyncio 기반)"""
        for attempt in range(max_retries):
            try:
                # 비동기 API 직접 호출
                # (API 제한 확인은 kiwoom_rest.py 내에서 처리됨)
                data = await self.trader.client.get_stock_minute_chart(code, period=3)
                
                if data:
                    return data
                else:
                    self.logger.warning(f"⚠️ [API 지연 디버깅] 분봉 차트 빈 데이터 응답! (시도 {attempt + 1}/{max_retries}) - 키움 서버가 데이터를 주지 않았습니다. 2초 후 재시도합니다.")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2)
                    
            except Exception as e:
                self.logger.warning(f"⚠️ [API 지연 디버깅] 분봉 데이터 수집 시도 {attempt + 1}/{max_retries} 실패: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
        
        return None
    
    def _on_chart_data_ready(self, code, tic_data, min_data):
        """차트 데이터 수집 완료 시그널 핸들러"""
        try:
            self.logger.debug(f"✅ 차트 데이터 수집 완료: {code} (tic: {tic_data is not None}, min: {min_data is not None})")
            
            # 캐시에 데이터 저장
            if code not in self.cache:
                # pending_stocks에 없으면(즉, 명시적으로 추가 요청된 상태가 아니면) 좀비 데이터로 간주하고 무시
                # 단, 이미 cache에 있는 경우는 업데이트이므로 통과
                if code not in self.pending_stocks:
                    self.logger.debug(f"🚫 {code}: 제거된 종목의 데이터 수신됨 - 캐시 저장 및 UI 추가 건너뜁니다.")
                    return

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
            
            # API 큐에서 처리된 종목을 모니터링 리스트에 추가
            if code in self.pending_stocks:
                stock_name = self.pending_stocks[code]
                if hasattr(self, 'parent') and self.parent:
                    # 이미 모니터링에 존재하는지 확인 (중복 추가 방지)
                    already_exists = False
                    if hasattr(self.parent, 'monitoring_manager'):
                        already_exists = code in self.parent.monitoring_manager.monitored_stocks
                    
                    # 존재하지 않을 때만 추가
                    if not already_exists:
                        # UI와 실시간 구독만 처리하도록 MonitoringManager 호출
                        create_fire_and_forget_task(self.parent.monitoring_manager.add_stock_to_monitoring(code, stock_name))
                        self.logger.debug(f"✅ 모니터링에 추가 완료: {code} - {stock_name}")
                
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
                # 루프에서 주기적으로 수집하므로 타이머 별도 시작은 불필요. 로그만 기록
                self.logger.debug(f"✅ 새 모니터링 종목 추가에 따른 API 대기열 인입 처리: {code}")
                
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
        """API 요청 큐 처리 (0.2초 간격, 대기열 로직 추가로 최대 1개 동시 수집 제어 -> 병목 및 429 방지)"""
        try:
            max_concurrent_tasks = 1
            
            if not self.api_request_queue:
                return
                
            if len(self.active_chart_tasks) >= max_concurrent_tasks:
                # 활성 수집 태스크 개수가 한계에 도달한 경우 다음 루프로 대기
                return
            
            # 큐에서 첫 번째 종목 가져오기
            code = self.api_request_queue.pop(0)
            
            # 이미 수집 중인 종목인 경우 건너뜀
            if code in self.active_chart_tasks:
                return
                
            self.logger.debug(f"🔧 큐에서 데이터 수집 시작: {code} (활성 태스크: {len(self.active_chart_tasks)}/{max_concurrent_tasks}, 남은 큐: {len(self.api_request_queue)}개)")
            
            # 차트 데이터 수집 비동기 실행 (force=True를 주어 조건 우회)
            self.update_single_chart(code, force=True)
            
        except Exception as ex:
            self.logger.error(f"❌ API 큐 처리 실패: {ex}")

    def remove_monitoring_stock(self, code):
        """모니터링 종목 제거 - 전체 정리 로직 위임"""
        self.remove_stock(code)
    
    def update_monitoring_stocks(self, codes):
        """모니터링 종목 리스트 업데이트 (캐시 동기화)"""
        try:
            current_codes = set(self.cache.keys())
            new_codes = set(codes)
            
            to_add = new_codes - current_codes
            to_remove = current_codes - new_codes
            
            # 변경사항이 없으면 조용히 리턴
            if not to_add and not to_remove:
                return

            self.logger.debug(f"🔧 모니터링 종목 리스트 동기화 시작")
            self.logger.debug(f"현재 캐시: {len(current_codes)}개, 목표: {len(new_codes)}개")
            
            # 추가할 종목 (순차적으로 처리)
            if to_add:
                self.logger.debug(f"추가할 종목: {list(to_add)}")
                self._add_monitoring_stocks_sequentially(list(to_add))
            
            # 제거할 종목
            if to_remove:
                self.logger.debug(f"제거할 종목: {list(to_remove)}")
                for code in to_remove:
                    self.remove_monitoring_stock(code)
            
            # 모니터링 종목 변경 로그
            if new_codes:
                logging.debug(f"✅ 모니터링 종목 동기화 완료: {len(new_codes)}개 유지")
            else:
                logging.warning("⚠️ 모니터링 종목이 없습니다 (전체 제거됨)")
                
        except Exception as ex:
            self.logger.error(f"❌ 모니터링 종목 리스트 업데이트 실패: {ex}", exc_info=True)
    
    def _start_queue_processing(self):
        """API 큐 처리 시작 (호환성용 유지)"""
        pass
    
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
    
    def update_single_chart(self, code, force=False):
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
            self.collect_chart_data_async(code, force=force)
            # finally 블록에서 queue_processing을 False로 설정하는 로직을 _collect_chart_data_internal로 이동
            
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 업데이트 실패: {code} - {ex}")
    

    
    def add_stock_to_api_queue(self, code):
        """종목을 API 큐에 추가 (차트 데이터 수집 후 모니터링에 추가)"""
        try:
            # 이미 모니터링에 존재하는지 확인
            if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'monitoring_manager'):
                if code in self.parent.monitoring_manager.monitored_stocks:
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
            
            # pending_stocks에서 제거 (중요: 재수신 방지)
            if code in self.pending_stocks:
                del self.pending_stocks[code]
                self.logger.debug(f"🗑️ ChartDataCache: {code} 대기 목록(pending_stocks)에서 제거됨")
            
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
            old_cache = self.cache.get(code, {})
            # 기존 캐시의 previous_close 값 유지
            previous_close = old_cache.get('previous_close', 0)
            
            # 실시간 전용 배열 보존 (API 조회 데이터에는 없는 필드들)
            if tic_data and 'tic_data' in old_cache and old_cache['tic_data']:
                old_tic = old_cache['tic_data']
                preserve_keys = ['buy_volume', 'sell_volume', 'strength', 'TICK_VELOCITY', 'LAST_TIC_CNT']
                new_len = len(tic_data.get('close', []))
                for p_key in preserve_keys:
                    if p_key in old_tic:
                        old_list = old_tic[p_key]
                        if len(old_list) == new_len:
                            tic_data[p_key] = old_list.copy()
                        elif len(old_list) > new_len:
                            tic_data[p_key] = old_list[-new_len:].copy()
                        else:
                            tic_data[p_key] = [0.0] * (new_len - len(old_list)) + old_list.copy()
            
            self.cache[code] = {
                'tic_data': tic_data,
                'min_data': min_data,
                'last_update': datetime.now(),
                'last_save': None,
                'previous_close': previous_close  # 전일종가 유지
            }
            
            if not tic_data:
                logging.warning(f"⚠️ [캐시 저장] {code} 종목의 틱 차트 데이터가 'None(데이터 없음)' 상태로 저장되었습니다. 향후 클릭 시 API 재요청(16초 지연)을 유발할 수 있습니다.")
            
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
                    time.sleep(wait_time)
                
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
                return self._aggregate_30_to_60_ticks(data)
                
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
    
    def _aggregate_30_to_60_ticks(self, data):
        """30틱 단위의 차트 데이터를 60틱 단위로 병합"""
        if not data or not isinstance(data, dict) or 'close' not in data or len(data['close']) == 0:
            return data
            
        n = len(data['close'])
        agg_data = {
            'time': [], 'open': [], 'high': [], 'low': [], 'close': [], 
            'volume': [], 'strength': [], 'last_tic_cnt': []
        }
        
        for i in range(0, n, 2):
            if i + 1 < n:
                # 2개의 30틱 캔들을 병합
                agg_data['time'].append(data['time'][i+1])
                agg_data['open'].append(data['open'][i])
                agg_data['high'].append(max(data['high'][i], data['high'][i+1]))
                agg_data['low'].append(min(data['low'][i], data['low'][i+1]))
                agg_data['close'].append(data['close'][i+1])
                agg_data['volume'].append(data['volume'][i] + data['volume'][i+1])
                
                s1 = data['strength'][i] if 'strength' in data and len(data['strength']) > i else 0
                s2 = data['strength'][i+1] if 'strength' in data and len(data['strength']) > i+1 else 0
                agg_data['strength'].append((s1 + s2) / 2)
                
                c1 = data['last_tic_cnt'][i] if 'last_tic_cnt' in data and len(data['last_tic_cnt']) > i else 30
                c2 = data['last_tic_cnt'][i+1] if 'last_tic_cnt' in data and len(data['last_tic_cnt']) > i+1 else 30
                
                try:
                    c1_val = int(c1) if c1 and str(c1).strip() else 30
                except (ValueError, TypeError):
                    c1_val = 30
                try:
                    c2_val = int(c2) if c2 and str(c2).strip() else 30
                except (ValueError, TypeError):
                    c2_val = 30
                    
                agg_data['last_tic_cnt'].append(c1_val + c2_val)
            else:
                # 마지막 홀수 캔들 (아직 60틱이 안 된 상태)
                agg_data['time'].append(data['time'][i])
                agg_data['open'].append(data['open'][i])
                agg_data['high'].append(data['high'][i])
                agg_data['low'].append(data['low'][i])
                agg_data['close'].append(data['close'][i])
                agg_data['volume'].append(data['volume'][i])
                
                s1 = data['strength'][i] if 'strength' in data and len(data['strength']) > i else 0
                agg_data['strength'].append(s1)
                
                c1 = data['last_tic_cnt'][i] if 'last_tic_cnt' in data and len(data['last_tic_cnt']) > i else 30
                try:
                    c1_val = int(c1) if c1 and str(c1).strip() else 30
                except (ValueError, TypeError):
                    c1_val = 30
                agg_data['last_tic_cnt'].append(c1_val)
                
        self.logger.debug(f"🔄 30틱 데이터를 60틱으로 병합 완료: {n}개 -> {len(agg_data['close'])}개")
        return agg_data
    
    def get_min_data_from_api(self, code, max_retries=3):
        """3분봉 데이터 조회 (재시도 로직 포함)"""
        
        for attempt in range(max_retries):
            try:
                # API 요청 간격 조정 (첫 번째 시도가 아닌 경우 대기)
                if attempt > 0:
                    wait_time = 2 ** attempt  # 지수 백오프: 2초, 4초, 8초
                    logging.debug(f"⏳ API 제한 대기 중... ({wait_time}초 후 재시도 {attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                
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
        """비동기 데이터베이스 저장 트리거 (비동기 태스크 시작)"""
        try:
            create_fire_and_forget_task(self.save_to_database())
            logging.debug("✅ DB 저장을 비동기 태스크로 시작")
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

                # 1분마다 저장 (마지막 저장 시간 확인)
                last_save = data.get('last_save')
                if last_save:
                    time_diff = (current_time - last_save).total_seconds()
                    if time_diff < 59:  # 59초 미만일 때만 건너뜀 (60초 타이밍 이슈 방지)
                        logging.debug(f"⏰ {code}: 아직 저장 시간이 안 됨 (경과: {time_diff:.1f}초, 마지막 저장: {last_save})")
                        continue
                
                logging.debug(f"💾 {code}: DB 저장 시작")

                # === [1. 실시간 스냅샷 저장 로직] ===
                queue = data.get('db_save_queue', [])
                if queue:
                    rows_to_save = queue[:]
                    data['db_save_queue'] = []
                    if hasattr(self.trader.db_manager, 'save_realtime_snapshots'):
                        await self.trader.db_manager.save_realtime_snapshots(code, rows_to_save)
                        saved_count += 1
                        logging.debug(f"✅ {code}: 실시간 스냅샷 DB 저장 완료 ({len(rows_to_save)}건)")

                # === [2. 과거 데이터 일괄 수집 모드 로직 (옵션)] ===
                from config_manager import EnvConfigParser
                data_collection_mode = EnvConfigParser().getboolean('DATA_SAVING', 'DATA_COLLECTION_MODE', fallback=False)
                
                if data_collection_mode:
                    # DataFrame 변환 및 지표 계산 (CPU 바운드 작업을 스레드풀로 오프로드)
                    try:
                        loop = asyncio.get_running_loop()
                        tic_df, min_df = await loop.run_in_executor(
                            None, 
                            self._prepare_data_for_db, 
                            data.get('tic_data'), 
                            data.get('min_data')
                        )
                    except Exception as df_ex:
                        self.logger.error(f"DB 저장용 데이터프레임 변환/지표 계산 실패 ({code}): {df_ex}")
                        continue
                    
                    if not tic_df.empty and not min_df.empty:
                        # 메모리에서 감시 시작 시간 가져오기
                        monitoring_start_time = None
                        if hasattr(self.parent, 'core_managers') and self.parent.core_managers:
                            monitoring_start_time = self.parent.core_managers.stock_added_time.get(code)
                        
                        # 중복 로그 방지
                        if not hasattr(self, '_log_data_collect') or code not in self._log_data_collect:
                            if not hasattr(self, '_log_data_collect'): self._log_data_collect = set()
                            logging.info(f"⚠️ [{code}] 데이터 수집 모드(DATA_COLLECTION_MODE) 켜짐 - 오늘 치 과거 데이터를 전부 강제 저장합니다.")
                            self._log_data_collect.add(code)
                        
                        await self.trader.db_manager.save_stock_data(
                            code, 
                            tic_df.to_dict('list'), 
                            min_df.to_dict('list'),
                            None  # 전체 저장을 위해 monitoring_start_time 해제
                        )
                        saved_count += 1
                        logging.debug(f"✅ {code}: 과거 데이터 일괄 저장 완료")

                # 저장 시간 업데이트
                data['last_save'] = current_time
                
                # 각 종목 처리 후 이벤트 루프에 제어권 반환 (PING 타임아웃 방지)
                await asyncio.sleep(0.05)

            
            if saved_count > 0:
                logging.debug(f"📊 통합 차트 데이터 DB 저장 완료: {saved_count}개 종목")
            else:
                logging.debug(f"ℹ️ 저장된 차트 데이터(틱/분봉)가 없습니다 (모니터링 종목: {len(self.cache)}개)")
                
        except Exception as ex:
            logging.error(f"통합 차트 데이터 DB 저장 실패: {ex}", exc_info=True)

    def _prepare_data_for_db(self, original_tic_data, original_min_data):
        """DB 저장을 위한 Pandas 데이터프레임 생성 및 지표 계산 (CPU 바운드 로직)"""
        tic_df = pd.DataFrame()
        if original_tic_data:
            valid_lists = [v for v in original_tic_data.values() if isinstance(v, list) and len(v) > 0]
            if valid_lists:
                min_len = min(len(v) for v in valid_lists)
                # GIL 블로킹(과부하) 방지를 위해 최근 1000건만 DataFrame으로 변환
                slice_len = min(min_len, 1000)
                # 가장 뒷부분(최신) 데이터를 가져오도록 수정
                trimmed_data = {k: v[-slice_len:] for k, v in original_tic_data.items() if isinstance(v, list)}
                tic_df = pd.DataFrame(trimmed_data)

        min_df = pd.DataFrame()
        if original_min_data:
            valid_lists = [v for v in original_min_data.values() if isinstance(v, list) and len(v) > 0]
            if valid_lists:
                min_len = min(len(v) for v in valid_lists)
                # GIL 블로킹 방지를 위해 최근 300건만 DataFrame으로 변환
                slice_len = min(min_len, 300)
                # 가장 뒷부분(최신) 데이터를 가져오도록 수정
                trimmed_data = {k: v[-slice_len:] for k, v in original_min_data.items() if isinstance(v, list)}
                min_df = pd.DataFrame(trimmed_data)

        if not tic_df.empty:
            tic_allowed = [
                'MA5', 'MA10', 'MA20', 'MA60', 'MA120', 
                'RSI', 'RSI_SIGNAL', 
                'VELOCITY', 'LAST_TIC_CNT'
            ]
            tic_indicators = strategy_utils.KiwoomIndicatorExtractor.extract_chart_indicators(tic_df, allowed_indicators=tic_allowed)
            base_cols = {'open', 'high', 'low', 'close', 'volume'}
            for key, value in tic_indicators.items():
                if key not in base_cols: tic_df[key] = value

        if not min_df.empty:
            min_allowed = ['MA5', 'MA10', 'MA20', 'MA60', 'MA120', 'RSI', 'RELATIVE_POSITION']
            min_indicators = strategy_utils.KiwoomIndicatorExtractor.extract_chart_indicators(min_df, allowed_indicators=min_allowed)
            base_cols = {'open', 'high', 'low', 'close', 'volume'}
            for key, value in min_indicators.items():
                if key not in base_cols: min_df[key] = value

        return tic_df, min_df

    
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
            # 모니터링 종목 수에 따라 동적으로 업데이트 주기 조절
            self.update_chart_update_interval()
            self.logger.debug("✅ ChartDataCache 백그라운드 스케줄러 동적 설정 완료")
        except Exception as ex:
            self.logger.error(f"❌ ChartDataCache 시작 실패: {ex}", exc_info=True)


    def stop(self):
        """캐시 정리"""
        try:
            # 태스크 중지는 stop()에서 처리하므로 캐시만 비움
            self.cache.clear()
            logging.debug("📊 차트 데이터 캐시 정리 완료")
        except Exception as ex:
            logging.error(f"❌ 차트 데이터 캐시 정리 실패: {ex}", exc_info=True)

    def update_chart_update_interval(self):
        """모니터링 종목 수에 따라 차트 업데이트 주기를 동적으로 조절합니다. (Pure Python)"""
        try:
            monitoring_codes = self.parent.monitoring_manager.get_monitoring_stock_codes()
            num_stocks = len(monitoring_codes)

            if num_stocks > 0:
                # 종목당 30초 간격으로 설정하고, 최소 주기를 300초(5분)로 제한하여 API 429 에러 방지 마진 확보
                new_interval_seconds = max(num_stocks * 30, 300)
                if hasattr(self.trader, 'chartdata_update_interval'):
                    if self.trader.chartdata_update_interval != new_interval_seconds:
                        self.trader.chartdata_update_interval = new_interval_seconds
                        self.logger.debug(f"🔄 차트 업데이트 주기 변경: {num_stocks}개 종목 * 30초 = {new_interval_seconds}초 (최소 300초)")
            else:
                # 종목이 없으면 기본 300초(5분)로 설정
                if hasattr(self.trader, 'chartdata_update_interval'):
                    self.trader.chartdata_update_interval = 300
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
            
            # 지표 계산 결과를 담을 딕셔너리
            indicators = {}

            # 허용된 지표 정의 (화이트리스트)
            if chart_type == "tic":
                allowed_set = {
                    'MA5', 'MA10', 'MA20', 'MA60', 'MA120',
                    'RSI', 'RSI_SIGNAL', 'RSI21',
                    'MACD', 'MACD_SIGNAL', 'MACD_HIST'
                }
            elif chart_type == "minute":
                allowed_set = {
                    'MA5', 'MA10', 'MA20',
                    'RSI', 'RELATIVE_POSITION', 'RSI21',
                    'MACD', 'MACD_SIGNAL', 'MACD_HIST'
                }
            else:
                allowed_set = {'MA5', 'MA20', 'MA60', 'RSI'}

            # 이동평균선
            for period in [5, 10, 20, 50, 60, 120]:
                ma_key = f'MA{period}'
                if ma_key in allowed_set and len(close_array) >= period:
                    indicators[ma_key] = talib.SMA(close_array, timeperiod=period)
            
            # RSI
            if 'RSI' in allowed_set and len(close_array) >= 15:
                # RSI 계산 (기본 14일)
                indicators['RSI'] = talib.RSI(close_array, timeperiod=14)
                
                # RSI Signal (RSI의 9일 이동평균)
                if 'RSI_SIGNAL' in allowed_set:
                    chart_len = len(indicators['RSI'])
                    if chart_len >= 9:
                        # 전체 길이에 대해 SMA 계산 (NaN은 전파됨)
                        indicators['RSI_SIGNAL'] = talib.SMA(indicators['RSI'], timeperiod=9)

            # RSI 21 (추가 요청)
            if 'RSI21' in allowed_set and len(close_array) >= 22:
                indicators['RSI21'] = talib.RSI(close_array, timeperiod=21)

            # MACD (허용된 경우만)
            if 'MACD' in allowed_set and len(close_array) >= 26:
                macd, macd_signal, macd_hist = talib.MACD(close_array)
                indicators['MACD'] = macd
                indicators['MACD_SIGNAL'] = macd_signal
                indicators['MACD_HIST'] = macd_hist
                
            # (삭제됨) 볼린저 밴드
                
            # 스토캐스틱 (허용된 경우만)
            if 'STOCH_K' in allowed_set and len(high_array) >= 14:
                slowk, slowd = talib.STOCH(high_array, low_array, close_array)
                indicators['STOCH_K'] = slowk
                indicators['STOCH_D'] = slowd

            # 이격도 (RELATIVE_POSITION)
            if 'RELATIVE_POSITION' in allowed_set and 'MA20' in indicators:
                ma20 = indicators['MA20']
                with np.errstate(divide='ignore', invalid='ignore'):
                    rel_pos = (close_array - ma20) / ma20
                indicators['RELATIVE_POSITION'] = rel_pos

            # ==========================================
            # 틱 차트 전용 지표 백필 (Backfill) & 병합
            # ==========================================
            if chart_type == "tic":
                try:
                    # 1. TICK_VELOCITY (10틱당 소요 시간 ms)
                    # 계산 (백필용)
                    time_list = data.get('time', [])
                    calculated_velocities = np.zeros(min_len)
                    
                    if len(time_list) >= min_len:
                        time_subset = time_list[:min_len]
                        pd_times = pd.to_datetime(time_subset, format='%Y%m%d%H%M%S', errors='coerce')
                        # DatetimeIndex.diff() returns TimedeltaIndex which has total_seconds() directly (no .dt accessor)
                        diffs = pd_times.diff().total_seconds() * 1000
                        calculated_velocities = (diffs / 6.0).fillna(0).to_numpy()

                    # 병합: 기존 데이터가 있으면 유지 (0이 아닌 값), 없으면 백필값 사용
                    existing_velocity = np.array(data.get('TICK_VELOCITY', []), dtype=float)
                    
                    if len(existing_velocity) >= min_len:
                        # 길이 맞추기
                        existing_velocity = existing_velocity[:min_len]
                        # 기존 값이 0이면 계산값 사용, 아니면 기존값 유지
                        # 단, 계산값이 0인 경우(첫번째 봉 등)는 0 유지
                        final_vel = np.where(existing_velocity == 0, calculated_velocities, existing_velocity)
                        indicators['TICK_VELOCITY'] = final_vel
                    else:
                        # 기존 데이터가 없거나 짧으면 계산값 전적 사용
                        indicators['TICK_VELOCITY'] = calculated_velocities


                    # 2. LAST_TIC_CNT (틱 카운트)
                    existing_cnt = np.array(data.get('LAST_TIC_CNT', []), dtype=float)
                    backfill_cnt = np.full(min_len, 60, dtype=int)
                    
                    if len(existing_cnt) >= min_len:
                        existing_cnt = existing_cnt[:min_len]
                        # 0이면 60(백필), 아니면 기존값
                        final_cnt = np.where(existing_cnt == 0, backfill_cnt, existing_cnt)
                        indicators['LAST_TIC_CNT'] = final_cnt
                    else:
                        indicators['LAST_TIC_CNT'] = backfill_cnt

                except Exception as vel_ex:
                    logging.debug(f"틱 지표 백필/병합 실패: {vel_ex}")

            # 데이터에 지표 직접 추가
            for key, value in indicators.items():
                if isinstance(value, np.ndarray):
                    data[key] = value.tolist()
                else:
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
                            
            # 최고가 대비 10% 하락 시 자체 이탈 처리 로직
            current_price_str = realtime_data.get('현재가', '0')
            current_price = abs(int(current_price_str)) if current_price_str not in ['', None] else 0
            
            if current_price > 0:
                if stock_code not in self.monitoring_highest_prices:
                    self.monitoring_highest_prices[stock_code] = current_price
                elif current_price > self.monitoring_highest_prices[stock_code]:
                    self.monitoring_highest_prices[stock_code] = current_price
                
                highest = self.monitoring_highest_prices[stock_code]
                if current_price <= highest * 0.90:
                    self.logger.info(f"📉 [{stock_code}] 최고가({highest:,}원) 대비 10% 하락 감지! 자체 이탈 처리합니다. (현재가: {current_price:,}원)")
                    
                    # 보유 종목인지 확인
                    is_holding = False
                    if hasattr(self, 'trader') and self.trader:
                        portfolio = self.trader.get_portfolio_status()
                        if stock_code in portfolio.get('holdings', {}):
                            is_holding = True
                            
                            
                    # 1. DB 이탈 기록 제거됨 (monitoring_history 사용 안함)
                    

                    # 2. 매수 차단 목록(Blacklist) 추가
                    if hasattr(self, 'trader') and self.trader:
                        if hasattr(self.trader, 'add_to_blacklist'):
                            self.trader.add_to_blacklist(stock_code, reason="최고가 대비 10% 하락 (모멘텀 상실)")
                        if hasattr(self.trader, 'condition_excluded_stocks'):
                            self.trader.condition_excluded_stocks.add(stock_code)
                        
                    if is_holding:
                        self.logger.debug(f"✅ 보유 종목이므로 모니터링은 유지합니다: {stock_code}")
                    else:
                        # 3. 감시 제외 (Core Manager / Monitoring Manager)
                        if hasattr(self, 'parent') and self.parent:
                            if hasattr(self.parent, 'monitoring_manager'):
                                await self.parent.monitoring_manager.remove_stock_from_monitoring(stock_code)
                            elif hasattr(self.parent, 'core_manager'):
                                self.parent.core_manager.remove_monitoring_stock(stock_code)
                        
                        # 캐시에서 자신을 직접 제거
                        self.remove_stock(stock_code)
                        return
            
            # 차트 캐시 업데이트 (메트릭 포함)
            chart_cache.cache[stock_code] = cached_data

            # 실시간 데이터를 틱/분봉 데이터에 추가
            is_new_candle = self.parent.login_handler.websocket_client._update_tic_chart_with_realtime(stock_code, cached_data, realtime_data)
            self.parent.login_handler.websocket_client._update_minute_chart_with_realtime(stock_code, cached_data, realtime_data)

            # 실시간 기술적 지표 계산 (비동기, ThreadPoolExecutor 사용 - 단, 1초 스로틀링 적용 또는 새 봉 생성 시 강제 업데이트)
            current_time = time.time()
            if current_time - self.last_indicator_calc_time.get(stock_code, 0) >= 1.0 or is_new_candle:
                self.last_indicator_calc_time[stock_code] = current_time
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

            # === [스냅샷 저장 로직 추가] ===
            if is_new_candle:
                try:
                    tic_data = cached_data.get('tic_data', {})
                    min_data = cached_data.get('min_data', {})
                    
                    if tic_data and min_data and len(tic_data.get('time', [])) >= 2 and len(min_data.get('time', [])) >= 1:
                        snapshot = {
                            'code': stock_code,
                            'datetime': tic_data['time'][-2]  # 방금 완성된 60틱봉의 시작/생성시간
                        }
                        
                        # 틱봉 지표 저장 (-2 인덱스: 완성된 봉)
                        for k, v in tic_data.items():
                            if isinstance(v, list) and len(v) >= 2:
                                if k == 'TICK_VELOCITY':
                                    col_name = 'tic_velocity'
                                else:
                                    col_name = f"tic_{k.lower()}" if k.lower() != 'time' else 'datetime'
                                if col_name != 'datetime':
                                    snapshot[col_name] = v[-2]
                        
                        # 3분봉 지표 저장 (-1 인덱스: 현재 찰나의 미완성 봉)
                        for k, v in min_data.items():
                            if isinstance(v, list) and len(v) >= 1:
                                col_name = f"min3_{k.lower()}" if k.lower() != 'time' else None
                                if col_name:
                                    snapshot[col_name] = v[-1]
                        
                        # 큐에 추가
                        if 'db_save_queue' not in cached_data:
                            cached_data['db_save_queue'] = []
                        cached_data['db_save_queue'].append(snapshot)
                except Exception as snap_ex:
                    self.logger.error(f"스냅샷 생성 실패 ({stock_code}): {snap_ex}")

            # 새 캔들이 완성되었을 경우 매수 신호 평가 이벤트를 비동기로 백그라운드 발생 (하이브리드 아키텍처)
            if is_new_candle and hasattr(self.parent, 'autotrader') and self.parent.autotrader:
                try:
                    from utils import create_fire_and_forget_task
                    create_fire_and_forget_task(self.parent.autotrader._analyze_and_execute_trading_async(stock_code, is_buy_check_allowed=True))
                except Exception as eval_ex:
                    self.logger.error(f"매수 평가 비동기 이벤트 발생 실패: {eval_ex}")

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
            
            cached_data['realtime_metrics']['total_sell_hoga'] = total_sell_hoga
            cached_data['realtime_metrics']['total_buy_hoga'] = total_buy_hoga
            
            # 1~3호가 잔량 추가
            cached_data['realtime_metrics']['sell_hoga_1'] = order_book_data.get('sell_hoga_1', 0)
            cached_data['realtime_metrics']['sell_hoga_2'] = order_book_data.get('sell_hoga_2', 0)
            cached_data['realtime_metrics']['sell_hoga_3'] = order_book_data.get('sell_hoga_3', 0)
            cached_data['realtime_metrics']['buy_hoga_1'] = order_book_data.get('buy_hoga_1', 0)
            cached_data['realtime_metrics']['buy_hoga_2'] = order_book_data.get('buy_hoga_2', 0)
            cached_data['realtime_metrics']['buy_hoga_3'] = order_book_data.get('buy_hoga_3', 0)
            
            # 차트 캐시 업데이트
            chart_cache.cache[stock_code] = cached_data
            
             # 데이터 업데이트 시그널 발생 (필요 시)
            # self.data_updated.emit(stock_code) # 호가 변경만으로 차트를 다시 그릴 필요가 없다면 주석 처리
            
        except Exception as e:
            self.logger.error(f"실시간 호가 데이터 처리 실패: {e}", exc_info=True)


