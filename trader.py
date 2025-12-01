import logging
import asyncio
import concurrent.futures
import configparser
import time
from datetime import datetime
from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from qasync import asyncSlot

from database import AsyncDatabaseManager
from utils import ApiLimitManager

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

                # 매도 주문 체결 추적
                ord_no = self.client.last_order_no
                if ord_no:
                    self.sell_order_details[ord_no] = {
                        'code': code,
                        'total_qty': quantity,
                        'filled_qty': 0,
                        'is_full_sale': is_full_sell
                    }
                    self.logger.debug(f"📋 매도 주문 추적 시작: 주문번호={ord_no}, 종목={code}, 수량={quantity}주, 전량매도={is_full_sell}")
                
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
        """self.holdings를 웹소켓 잔고 데이터와 동기화""" # type: ignore
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

    async def get_account_balance(self) -> dict:
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
            self.daily_report_sent = False # 15:30 장마감 리포트 전송 여부
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

    async def _execute_daily_report_async(self):
        """15:30 장 마감 리포트 실행"""
        try:
            self.logger.info("🕒 15:30 장 마감 - 최종 실현 손익 리포트 전송 시작")
            if hasattr(self.parent, 'login_handler') and self.parent.login_handler.kiwoom_client:
                total_profit, total_profit_rate = await self.parent.login_handler.kiwoom_client.get_daily_realized_profit()
                await self.parent.login_handler.kiwoom_client.send_slack_daily_report(total_profit, total_profit_rate)
        except Exception as ex:
            self.logger.error(f"❌ 장 마감 리포트 실행 중 오류: {ex}", exc_info=True)
    
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

            # 15:30 장 마감 리포트 전송 (1회만 실행)
            if current_time_str == "15:30" and not self.daily_report_sent:
                await self._execute_daily_report_async()
                self.daily_report_sent = True
            
            # 다음날을 위해 플래그 리셋 (15:31 이후)
            if current_time_str == "15:31" and self.auto_liquidation_executed:
                self.auto_liquidation_executed = False
                self.daily_report_sent = False
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
