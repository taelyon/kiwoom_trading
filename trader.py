import logging
import asyncio
import concurrent.futures
from config_manager import EnvConfigParser
import time
import json
import os
from datetime import datetime

from database import AsyncDatabaseManager
from utils import ApiLimitManager, create_fire_and_forget_task

# ==================== 키움 트레이더 클래스 ====================
class KiwoomTrader:
    """키움 REST API 기반 트레이더 클래스 (Pure Python)"""
    
    def __init__(self, client, buycount, parent=None):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client = client
        self.buycount = buycount
        self.parent = parent
        self.db_manager = AsyncDatabaseManager()
        self._init_database_async()
        
        # 포트폴리오 관리
        self.holdings = {}  # 보유 종목
        self.buy_prices = {}  # 매수 가격
        self.buy_times = {}  # 매수 시간
        self.highest_prices = {}  # 최고가 추적

        # 최근 전량 매도한 종목 추적 (UI 재출현 방지용, {code: timestamp})
        self.sold_blacklist = {}

        # 매도 주문 진행 중인 종목 추적 (중복 매도 방지)
        self.pending_sell_orders = set()

        # 주문 번호별 매도 정보 추적 (부분 매도 완료 알림용)
        self.sell_order_details = {}  # {order_no: {'code': str, 'total_qty': int, 'filled_qty': int}}

        # 임시 매도 요청 기록 (주문번호 발급 전 웹소켓 수신 대비)
        self.temp_sell_logs = {} # {code: {'quantity': qty, 'timestamp': datetime, 'strategy': str}}

        # 임시 매수 요청 기록
        self.temp_buy_logs = {} # {code: {'quantity': qty, 'timestamp': datetime, 'strategy': str}}
        
        # 주문번호별 전략 매핑 (접수 시 매핑됨)
        self.order_strategies = {} # {order_no: strategy_name}

        # 이미 발동된 매도 룰 추적 (중복 매도 방지, {code: set(['룰이름1', ...])})
        self.executed_sell_rules = {}

        # 매수 주문 진행 중인 종목 추적 (중복 매수 방지)
        self.pending_buy_orders = set()
        
        # 웹소켓 실시간 데이터 저장소
        self.balance_data = {}  # 웹소켓 실시간 잔고 데이터
        self.execution_data = {}  # 웹소켓 실시간 체결 데이터
        
        # 현금 조회 캐시 (API 호출 빈도 제한)
        # 투자원금: .env에 사용자가 설정한 값을 최우선 사용 (API fallback 방지)
        self.config = EnvConfigParser()
        env_prime_cash = self.config.getint('SETTINGS', 'prime_cash', fallback=0)
        self.prime_cash = env_prime_cash
        self._cash_cache = 0.0
        self._cash_cache_time = 0
        # 예수금 조회 동시성 제어를 위한 Lock
        self._cash_query_lock = asyncio.Lock()

        # 종목별 매수 주문 동시성 제어를 위한 Lock
        self._buy_order_locks = {}

        # 당일 매수 금지 종목 (추적손절 등으로 매도된 종목)
        self.condition_excluded_stocks = set() # 자체 이탈(10% 등)로 모니터링에서 제거된 종목들의 집합
        self.daily_blacklist = set()
        self.cooldown_list = {}  # {stock_code: cooldown_expire_timestamp}
        self.load_blacklist()  # 파일에서 블랙리스트 복원
        self.load_settings()

        self.logger.debug(f"키움 트레이더 초기화 완료 (목표 매수 종목 수: {self.buycount})")
    
    def _init_database_async(self):
        """비동기 데이터베이스 초기화 트리거"""
        try:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.db_manager.init_database())
                self.logger.debug("✅ DB 초기화를 비동기 태스크로 시작")
                return
            except RuntimeError:
                self.logger.debug("⚠️ 실행 중인 이벤트 루프가 없어 ThreadPoolExecutor 사용")
                pass
            
            def run_async_init():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        return loop.run_until_complete(self.db_manager.init_database())
                    finally:
                        loop.close()
                except Exception as e:
                    self.logger.error(f"비동기 데이터베이스 초기화 실행 오류: {e}", exc_info=True)
                    return None
            
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_async_init)
                future.result(timeout=30)
                
        except Exception as ex:
            self.logger.error(f"비동기 데이터베이스 초기화 트리거 실패: {ex}", exc_info=True)
    
    def load_settings(self):
        """설정 로드"""
        try:
            config = EnvConfigParser()
            self.evaluation_interval = config.getint('TRADING', 'evaluation_interval', fallback=1)

            commission_rate_str = config.get('TRADING', 'commission_rate', fallback='0.00015')
            tax_rate_str = config.get('TRADING', 'tax_rate', fallback='0.0018')
            
            self.commission_rate = float(commission_rate_str.split(';')[0].strip())
            self.tax_rate = float(tax_rate_str.split(';')[0].strip())
            self.min_hold_seconds = config.getint('TRADING', 'min_hold_seconds', fallback=0)
            self.data_saving_interval = config.getint('DATA_SAVING', 'interval_seconds', fallback=60)
            self.chartdata_update_interval = config.getint('CHART', 'chartdata_update_interval', fallback=300)
            self.logger.debug("설정 로드 완료")
        except Exception as ex:
            self.logger.error(f"설정 로드 실패: {ex}", exc_info=True)
    
    def add_to_blacklist(self, code, reason=""):
        """종목을 당일 매수 금지 목록에 추가"""
        self.daily_blacklist.add(code)
        self.save_blacklist()
        log_msg = f"🚫 [{code}] 당일 매수 금지 목록(Blacklist)에 추가됨"
        if reason:
            log_msg += f" (사유: {reason})"
        self.logger.debug(log_msg)

    def add_to_cooldown(self, code, duration_minutes=60):
        """종목을 지정된 시간(분) 동안 매수 금지(쿨타임) 목록에 추가"""
        expire_time = datetime.now() + timedelta(minutes=duration_minutes)
        self.cooldown_list[code] = expire_time.timestamp()
        self.logger.info(f"⏳ [{code}] 쿨타임 {duration_minutes}분 적용 (해제: {expire_time.strftime('%H:%M:%S')})")

    def is_blacklisted(self, code):
        """종목이 당일 매수 금지 목록 또는 현재 쿨타임 상태인지 확인"""
        if code in self.daily_blacklist:
            return True
            
        if code in self.cooldown_list:
            if datetime.now().timestamp() < self.cooldown_list[code]:
                return True
            else:
                # 쿨타임 해제
                del self.cooldown_list[code]
                self.logger.info(f"🔓 [{code}] 쿨타임 해제 완료 (재매수 가능)")
                return False
                
        return False

    def reset_blacklist(self):
        """당일 매수 금지 및 쿨타임 목록 초기화"""
        if self.daily_blacklist or self.cooldown_list:
            self.logger.info(f"🔄 당일 매수 금지 및 쿨타임 목록 초기화: 블랙리스트 {len(self.daily_blacklist)}개, 쿨타임 {len(self.cooldown_list)}개 해제")
            self.daily_blacklist.clear()
            self.cooldown_list.clear()
            self.save_blacklist()
        else:
            self.logger.debug("🔄 당일 매수 금지 목록 초기화 (목록 비어있음)")

    def load_blacklist(self):
        """파일에서 블랙리스트 로드"""
        try:
            if os.path.exists('blacklist.json'):
                with open('blacklist.json', 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                saved_date = data.get('date')
                current_date = datetime.now().strftime('%Y-%m-%d')
                
                if saved_date == current_date:
                    self.daily_blacklist = set(data.get('codes', []))
                    self.logger.info(f"📂 블랙리스트 복원 완료: {len(self.daily_blacklist)}개 종목 ({saved_date})")
                else:
                    self.logger.info(f"📂 지난 블랙리스트 파일 무시 (날짜 불일치: {saved_date} != {current_date})")
                    self.daily_blacklist.clear()
                    self.save_blacklist()
        except Exception as ex:
            self.logger.error(f"블랙리스트 로드 실패: {ex}")

    def save_blacklist(self):
        """블랙리스트를 파일에 저장"""
        try:
            data = {
                'date': datetime.now().strftime('%Y-%m-%d'),
                'codes': list(self.daily_blacklist)
            }
            with open('blacklist.json', 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as ex:
            self.logger.error(f"블랙리스트 저장 실패: {ex}")

    def is_recently_sold(self, code):
        """최근 전량 매도된 종목인지 확인"""
        if code in self.sold_blacklist:
            sold_time = self.sold_blacklist[code]
            if (datetime.now() - sold_time).total_seconds() < 10:
                return True
            else:
                del self.sold_blacklist[code]
        return False

    async def get_current_price(self, code):
        """현재가 조회"""
        try:
            price_data = await self.client.get_stock_current_price(code)
            return price_data.get('current_price', 0)
        except Exception as ex:
            self.logger.debug(f"현재가 조회 실패 ({code}) - fallback 처리됨", exc_info=True)
            return 0
    
    async def place_buy_order(self, code, quantity, price=0, strategy=""):
        """매수 주문"""
        if code not in self._buy_order_locks:
            self._buy_order_locks[code] = asyncio.Lock()
        lock = self._buy_order_locks[code]

        async with lock:
            try:
                if code in self.pending_buy_orders:
                    self.logger.debug(f"⏳ [{code}] 다른 작업이 이미 매수 주문을 진행 중이므로 건너뜁니다.")
                    return False

                # 1. 보유 종목 확인
                if code in self.holdings and self.holdings[code].get('quantity', 0) > 0:
                    self.logger.info(f"⚠️ 매수 주문 취소: {code}는 이미 보유 중인 종목입니다.")
                    return False
                
                # 2. 최대 보유 종목 수 확인
                max_count = self.buycount
                current_count = len([c for c, inf in self.holdings.items() if inf.get('quantity', 0) > 0])
                available_buy_count = max_count - current_count
                
                if available_buy_count <= 0:
                    self.logger.warning(f"⚠️ 매수 주문 취소: 최대 보유 종목 수 도달 ({code}) - {current_count}/{max_count}")
                    return False
                
                self.pending_buy_orders.add(code)
                self.logger.debug(f"⏳ [{code}] 매수 주문 진행 중 상태로 설정 (중복 주문 방지)")
                
                success = await self.client.place_buy_order(code, quantity, price)
                
                if success:
                    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    if code in self.pending_sell_orders:
                        self.pending_sell_orders.discard(code)
                    
                    self.temp_buy_logs[code] = {
                        'quantity': quantity,
                        'timestamp': datetime.now(),
                        'strategy': strategy
                    }
                    
                    self.buy_prices[code] = price if price > 0 else await self.get_current_price(code)
                    self.buy_times[code] = datetime.now()
                    self.highest_prices[code] = self.buy_prices[code]
                    
                    self.holdings[code] = {
                        'quantity': quantity,
                        'average_price': self.buy_prices[code],
                        'current_price': self.buy_prices[code]
                    }
                    
                    buy_strategy_name = strategy
                    if buy_strategy_name.startswith('['):
                        try:
                            buy_strategy_name = buy_strategy_name.split(']')[0][1:]
                        except Exception: pass
                    self.holdings[code]['buy_strategy'] = buy_strategy_name
                    
                    self.logger.debug(f"✅ holdings 업데이트: {code} (수량: {quantity}주, 평단: {self.buy_prices[code]:,}원)")
                    
                    # 자동 모니터링 등록
                    if self.parent and hasattr(self.parent, 'monitoring_manager'):
                        await self.parent.monitoring_manager.add_stock_to_monitoring(code)
                    
                    if self.parent and hasattr(self.parent, 'on_order_result'):
                        self.parent.on_order_result(code, "buy", quantity, price, True)
                    self.logger.debug(f"✅ 매수 주문 성공: {code} {quantity}주")
                    return True
                else:
                    if code in self.pending_buy_orders:
                        self.pending_buy_orders.discard(code)
                    if self.parent and hasattr(self.parent, 'on_order_result'):
                        self.parent.on_order_result(code, "buy", quantity, price, False)
                    self.logger.error(f"❌ 매수 주문 실패: {code}")
                    return False
                    
            except Exception as ex:
                self.logger.error(f"매수 주문 중 오류 ({code}): {ex}", exc_info=True)
                if code in self.pending_buy_orders:
                    self.pending_buy_orders.discard(code)
                if self.parent and hasattr(self.parent, 'on_order_result'):
                    self.parent.on_order_result(code, "buy", quantity, price, False)
                return False
    
    async def place_sell_order(self, code, quantity, price=0, strategy=""):
        """매도 주문"""
        try:
            if quantity <= 0:
                self.logger.warning(f"⚠️ 매도 주문 수량이 0 이하이므로 주문을 실행하지 않습니다: {code}, 수량: {quantity}")
                return False

            self.temp_sell_logs[code] = {
                'quantity': quantity,
                'timestamp': datetime.now(),
                'strategy': strategy
            }

            if code in self.pending_sell_orders:
                self.logger.warning(f"⏳ [{code}] 이미 매도 주문이 진행 중입니다. 중복 주문을 방지합니다.")
                return False

            self.pending_sell_orders.add(code)
            self.logger.debug(f"⏳ [{code}] 매도 주문 진행 중 상태로 설정 (중복 주문 방지)")

            success = await self.client.place_sell_order(code, quantity, price)
            
            if success:
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                # DB 기록은 kiwoom_websocket.py의 체결(Execution) 이벤트에서 처리됩니다.
                is_full_sell = False
                remaining_qty = 0
                
                if code in self.holdings:
                    remaining_qty = self.holdings[code].get('quantity', 0)
                    is_full_sell = (remaining_qty <= quantity)
                else:
                    balance_data = self.get_balance_data()
                    if code in balance_data.get('holdings', {}):
                        remaining_qty = balance_data['holdings'][code].get('quantity', 0)
                        is_full_sell = (remaining_qty <= quantity)
                    else:
                        is_full_sell = True
                
                if is_full_sell and code in self.highest_prices:
                    del self.highest_prices[code]
                    self.logger.debug(f"🗑️ {code} 최고가 정보 초기화 (전량 매도)")

                ord_no = self.client.last_order_no
                if ord_no:
                    self.sell_order_details[ord_no] = {
                        'code': code,
                        'total_qty': quantity,
                        'filled_qty': 0,
                        'is_full_sale': is_full_sell
                    }
                    self.logger.debug(f"📋 매도 주문 추적 시작: 주문번호={ord_no}, 종목={code}, 수량={quantity}주")
                
                if is_full_sell:
                    if code in self.holdings:
                        del self.holdings[code]
                    if code in self.buy_prices:
                        del self.buy_prices[code]
                    if code in self.buy_times:
                        del self.buy_times[code]
                    if code in self.executed_sell_rules:
                        del self.executed_sell_rules[code]
                        self.logger.debug(f"🧹 [{code}] 전량 매도로 분할 매도 이력 초기화")
                    
                    self.sold_blacklist[code] = datetime.now()
                    self.logger.debug(f"🚫 [{code}] 최근 전량 매도 목록에 추가 (10초간 UI 재출현 방지)")
                else:
                    if code in self.holdings:
                        new_quantity = remaining_qty - quantity
                        self.holdings[code]['quantity'] = new_quantity
                        self.logger.debug(f"✅ holdings 수량 업데이트: {code} ({remaining_qty}주 → {new_quantity}주)")
                
                if is_full_sell and (self.is_blacklisted(code) or code in self.condition_excluded_stocks):
                    if self.parent and hasattr(self.parent, 'monitoring_manager'):
                        await self.parent.monitoring_manager.remove_stock_from_monitoring(code)
                        self.logger.info(f"🗑️ [{code}] 전량 매도 완료 - 모니터링 제외")
                
                if self.parent and hasattr(self.parent, 'on_order_result'):
                    self.parent.on_order_result(code, "sell", quantity, price, True)
                
                if code in self.pending_sell_orders:
                    self.pending_sell_orders.discard(code)
                return True
            else:
                if code in self.pending_sell_orders:
                    self.pending_sell_orders.discard(code)
                if self.parent and hasattr(self.parent, 'on_order_result'):
                    self.parent.on_order_result(code, "sell", quantity, price, False)
                self.logger.error(f"❌ 매도 주문 실패: {code}")
                return False
                
        except Exception as ex:
            self.logger.error(f"매도 주문 중 오류 ({code}): {ex}", exc_info=True)
            if code in self.pending_sell_orders:
                self.pending_sell_orders.discard(code)
            if self.parent and hasattr(self.parent, 'on_order_result'):
                self.parent.on_order_result(code, "sell", quantity, price, False)
            return False
    
    def get_portfolio_status(self):
        """포트폴리오 상태 조회"""
        try:
            self._sync_holdings_with_websocket()

            merged_holdings = self.holdings.copy()
            merged_buy_prices = self.buy_prices.copy()
            merged_buy_times = self.buy_times.copy()
            
            try:
                ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
                if ws_client and hasattr(ws_client, 'balance_data') and ws_client.balance_data:
                    ws_balance_data = ws_client.balance_data
                    
                    codes_to_remove = []
                    for code in merged_holdings.keys():
                        if code not in ws_balance_data:
                            codes_to_remove.append(code)
                    
                    for code in codes_to_remove:
                        del merged_holdings[code]
                        if code in merged_buy_prices: del merged_buy_prices[code]
                        if code in merged_buy_times: del merged_buy_times[code]
                        # self.executed_sell_rules는 여기서 삭제하지 않음 (실제 holdings 갱신은 _sync에서 함)
                    
                    for code, balance_info in ws_balance_data.items():
                        if code == 'available_cash':
                            continue
                        quantity = balance_info.get('quantity', 0)
                        
                        if quantity == 0:
                            if code in merged_holdings:
                                del merged_holdings[code]
                            if code in merged_buy_prices: del merged_buy_prices[code]
                            if code in merged_buy_times: del merged_buy_times[code]
                        elif quantity > 0:
                            if code not in merged_holdings:
                                merged_holdings[code] = {'quantity': quantity}
                                if code not in merged_buy_prices:
                                    merged_buy_prices[code] = balance_info.get('average_price', 0)
                                if code not in merged_buy_times:
                                    merged_buy_times[code] = datetime.now()
                            else:
                                ws_quantity = quantity
                                holdings_quantity = merged_holdings[code].get('quantity', 0)

                                if code not in merged_buy_prices and code in self.buy_prices:
                                    merged_buy_prices[code] = self.buy_prices[code]
                                if code not in merged_buy_times and code in self.buy_times:
                                    merged_buy_times[code] = self.buy_times[code]

                                if ws_quantity != holdings_quantity:
                                    merged_holdings[code]['quantity'] = ws_quantity
                                    if code not in merged_buy_prices or merged_buy_prices[code] == 0:
                                        merged_buy_prices[code] = balance_info.get('average_price', 0)
            except Exception as ws_ex:
                self.logger.debug(f"웹소켓 balance_data 동기화 중 오류 (무시): {ws_ex}")
            
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
            ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
            if not ws_client or not hasattr(ws_client, 'balance_data') or not ws_client.balance_data:
                if self.holdings:
                    self.holdings.clear()
                    self.buy_prices.clear()
                    self.buy_times.clear()
                    self.highest_prices.clear()
                return

            ws_balance_data = ws_client.balance_data
            codes_in_holdings = set(self.holdings.keys())
            codes_in_websocket = set(ws_balance_data.keys())
            
            # available_cash 특수키 제외
            codes_in_websocket.discard('available_cash')
            
            for code in codes_in_holdings - codes_in_websocket:
                del self.holdings[code]
                if code in self.buy_prices: del self.buy_prices[code]
                if code in self.buy_times: del self.buy_times[code]
                if code in self.highest_prices: del self.highest_prices[code]
                if code in self.executed_sell_rules: del self.executed_sell_rules[code]

            for code, balance_info in ws_balance_data.items():
                if code == 'available_cash':
                    continue
                quantity = balance_info.get('quantity', 0)
                average_price = balance_info.get('average_price', 0)
                
                if quantity > 0:
                    if code not in self.holdings:
                        self.holdings[code] = {'quantity': quantity}
                        self.buy_prices[code] = average_price
                        self.buy_times[code] = datetime.now()
                    else:
                        if self.holdings[code]['quantity'] != quantity:
                            self.holdings[code]['quantity'] = quantity
                        
                        if average_price > 0 and code in self.buy_prices:
                            old_price = self.buy_prices[code]
                            if abs(old_price - average_price) > 0:
                                self.buy_prices[code] = average_price
                else:
                    if code in self.holdings:
                        del self.holdings[code]
                    if code in self.buy_prices: del self.buy_prices[code]
                    if code in self.buy_times: del self.buy_times[code]
                    if code in self.highest_prices: del self.highest_prices[code]
                    if code in self.executed_sell_rules: del self.executed_sell_rules[code]
        except Exception as ex:
            self.logger.warning(f"self.holdings와 웹소켓 잔고 동기화 실패: {ex}", exc_info=True)

    def get_balance_data(self):
        """웹소켓 실시간 잔고 데이터 조회"""
        if not self.balance_data:
            return {
                'available_cash': 0,
                'holdings': {},
                'total_assets': 0
            }
        return self.balance_data.copy()

    async def get_account_balance(self) -> dict:
        """투자계좌자산현황조회"""
        try:
            if not await self.client.check_token_validity():
                return {}
            
            server_url = self.client.mock_url if self.client.is_mock else self.client.base_url
            url = f"{server_url}/uapi/domestic-stock/v1/trading/inquire-account-balance"
            
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.client.access_token}',
                'appkey': self.client.app_key,
                'appsecret': self.client.app_secret,
                'tr_id': 'CTRP6548R',
            }
            
            params = {
                'CANO': self.client.account_number,
                'ACNT_PRDT_CD': self.client.account_product_code,
                'INQR_DVSN_1': '',
                'BSPR_BF_DT_APLY_YN': ''
            }
            
            await self.client._ensure_client()
            response = await self.client.post(url, headers=headers, json=params, timeout=10.0)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('rt_cd') == '0':
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
        """투자가능 현금 조회"""
        async with self._cash_query_lock:
            try:
                current_time = time.time()
                cache_validity_period = 5
                
                if (current_time - self._cash_cache_time) < cache_validity_period:
                    return self._cash_cache
                
                await ApiLimitManager.check_api_limit_and_wait_async("예수금상세현황 조회", request_type="deposit")
                
                deposit_data = await self.client.get_deposit_detail()
                if not deposit_data:
                    return self._cash_cache
                
                available_cash = float(deposit_data.get('ord_alow_amt', 0))
                self._cash_cache = available_cash
                self._cash_cache_time = current_time
                
                return available_cash
            except Exception as e:
                self.logger.error(f"투자가능 현금 조회 중 오류: {e}", exc_info=True)
                return self._cash_cache


# ==================== 자동매매 클래스 ====================
class AutoTrader:
    """자동매매 관리 클래스 (Pure Python / Asyncio loop)"""
    
    def __init__(self, trader, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.trader = trader            
        self.parent = parent            
        self.is_running = False
        self.auto_liquidation_executed = False
        self.daily_report_sent = False
        self._loop_task = None
        self.logger.debug("AutoTrader 초기화 완료")
        
    def start_auto_trading(self):
        """자동매매 시작 (비동기 루프 기동)"""
        if not self.is_running:
            self.is_running = True
            self._loop_task = asyncio.create_task(self._auto_trading_loop())
            self.logger.info("✅ 자동매매 비동기 루프 시작")

    def stop_auto_trading(self):
        """자동매매 중지"""
        if self.is_running:
            self.is_running = False
            if self._loop_task:
                self._loop_task.cancel()
                self._loop_task = None
            self.logger.info("🛑 자동매매 비동기 루프 중지")

    async def _auto_trading_loop(self):
        """asyncio 기반 주기적 매매 감시 루프"""
        while self.is_running:
            try:
                await self._periodic_trading_check()
                
                # 대기 주기 설정 (장중 1초 등, 장외 60초)
                now = datetime.now()
                current_time_minutes = now.hour * 60 + now.minute
                start_time_minutes = 9 * 60
                end_time_minutes = 15 * 60 + 30
                
                if start_time_minutes <= current_time_minutes < end_time_minutes:
                    await asyncio.sleep(self.trader.evaluation_interval)
                else:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"자동매매 루프 에러: {e}", exc_info=True)
                await asyncio.sleep(5)
    
    async def execute_auto_liquidation_async(self):
        """15:15 자동 청산 실행"""
        try:
            self.logger.info("🕒 15:15 자동 청산 로직 실행")
            if hasattr(self.parent, 'trading_manager'):
                await self.parent.trading_manager.sell_all_item(is_auto=True)
                
                wait_seconds = 0
                while self.trader.sell_order_details and wait_seconds < 60:
                    await asyncio.sleep(1)
                    wait_seconds += 1
                    if wait_seconds % 10 == 0:
                        self.logger.info(f"⏳ 자동 청산 매도 체결 대기 중... ({len(self.trader.sell_order_details)}건 남음)")
                
                if self.trader.sell_order_details:
                    self.logger.warning(f"⚠️ 일부 매도 주문 미체결 상태로 대기 시간 종료 ({len(self.trader.sell_order_details)}건)")

                self.logger.info("⏹️ 15:15 자동 청산 완료 - 모든 매매 활동 중지")
                
                if hasattr(self.parent, 'condition_search_manager'):
                    await self.parent.condition_search_manager.stop_all_conditions()
                    self.logger.info("⏹️ 조건검색 실시간 구독 해제 완료")
                
                if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                    self.parent.chart_cache.stop()
                    self.logger.info("⏹️ 차트 데이터 캐시 및 DB 저장 타이머 중지 완료")
        except Exception as ex:
            self.logger.error(f"❌ 자동 청산 실행 중 오류: {ex}", exc_info=True)

    async def _execute_daily_report_async(self):
        """15:30 장 마감 리포트 실행"""
        try:
            self.logger.info("🕒 15:30 장 마감 - 최종 실현 손익 리포트 전송 시작")
            self.logger.debug("⏳ 모든 매도 체결 처리 완료 대기 중... (5초)")
            await asyncio.sleep(5)
            
            if hasattr(self.parent, 'login_handler') and self.parent.login_handler.kiwoom_client:
                total_profit, total_profit_rate = await self.parent.login_handler.kiwoom_client.get_daily_realized_profit()
                await self.parent.login_handler.kiwoom_client.send_slack_daily_report(total_profit, total_profit_rate)
        except Exception as ex:
            self.logger.error(f"❌ 장 마감 리포트 실행 중 오류: {ex}", exc_info=True)
    
    async def _periodic_trading_check(self):
        """주기적 매매 판단 실행"""
        try:
            if not self.is_running:
                return
            
            now = datetime.now()
            current_time_str = now.strftime("%H:%M")
            current_hour = now.hour
            current_minute = now.minute
            
            # 15:15 자동 청산 체크
            if current_time_str == "15:15" and not self.auto_liquidation_executed:
                self.logger.info("🕒 15:15 도달 - 모든 보유 종목 자동 청산 시작")
                self.auto_liquidation_executed = True
                await self.execute_auto_liquidation_async()

            # 15:30 장 마감 리포트 전송
            if current_time_str == "15:30" and not self.daily_report_sent:
                self.daily_report_sent = True
                await self._execute_daily_report_async()
            
            # 다음날을 위한 리셋 (15:31)
            if current_time_str == "15:31" and self.auto_liquidation_executed:
                self.auto_liquidation_executed = False
                self.daily_report_sent = False
                logging.debug("🔄 자동 청산 플래그 리셋 완료")
            
            trading_start_time = (9, 0)
            trading_end_time = (15, 30)
            
            current_time_minutes = current_hour * 60 + current_minute
            start_time_minutes = trading_start_time[0] * 60 + trading_start_time[1]
            end_time_minutes = trading_end_time[0] * 60 + trading_end_time[1]
            
            # 장 시작 전 블랙리스트 초기화
            if current_time_minutes < start_time_minutes:
                if current_time_str == "08:50":
                     if not hasattr(self, '_blacklist_reset_done') or not self._blacklist_reset_done:
                        self.trader.reset_blacklist()
                        self._blacklist_reset_done = True
                elif current_time_str == "08:51":
                    if hasattr(self, '_blacklist_reset_done'):
                        delattr(self, '_blacklist_reset_done')
                return
            
            if current_time_minutes >= end_time_minutes:
                return
            
            if not hasattr(self.parent, 'chart_cache') or not self.parent.chart_cache:
                return
            
            if not hasattr(self, '_last_status_log_time'):
                self._last_status_log_time = 0
            
            current_time = time.time()
            if current_time - self._last_status_log_time >= 60:
                monitoring_codes = list(self.parent.chart_cache.cache.keys())
                if monitoring_codes:
                    self.logger.debug(f"🔍 자동매매 모니터링 중: {len(monitoring_codes)}개 종목 - {monitoring_codes}")
                else:
                    self.logger.debug("🔍 자동매매 실행 중 - 모니터링 종목 없음")
                self._last_status_log_time = current_time
            

            if self.auto_liquidation_executed:
                return
            
            codes = list(self.parent.chart_cache.cache.keys())
            if codes:
                # GIL(Global Interpreter Lock) 경합 방지 및 이벤트 루프(웹소켓 등) 반응성 확보를 위해
                # asyncio.gather 동시 실행 대신 순차 실행 + 짧은 sleep 적용
                for code in codes:
                    try:
                        await self._analyze_and_execute_trading_async(code)
                        # 다른 비동기 태스크(웹소켓 수신, API 응답 등)에 제어권 양보
                        await asyncio.sleep(0.01)
                    except Exception as e:
                        self.logger.error(f"종목 {code} 매매 판단 중 오류: {e}")
        except Exception as ex:
            self.logger.error(f"주기적 매매 판단 중 오류: {ex}", exc_info=True)
    
    async def _analyze_and_execute_trading_async(self, code, is_buy_check_allowed=False):
        """매매 판단 및 실행 (비동기)"""
        try:
            await self.analyze_and_execute_trading(code, is_buy_check_allowed=is_buy_check_allowed)
        except Exception as ex:
            self.logger.error(f"비동기 매매 판단 실패 ({code}): {ex}", exc_info=True)
    
    async def analyze_and_execute_trading(self, code, is_buy_check_allowed=False):
        """매매 판단 실행"""
        try:
            if self.auto_liquidation_executed:
                return False
            
            now = datetime.now()
            current_time_minutes = now.hour * 60 + now.minute
            if current_time_minutes < 540 or current_time_minutes >= 930:
                return False
            
            if not hasattr(self, '_analyze_debug_codes'):
                self._analyze_debug_codes = set()
            
            is_first_debug = code not in self._analyze_debug_codes
            if is_first_debug:
                self.logger.debug(f"🔍 [{code}] 매매 판단 시작")
                self._analyze_debug_codes.add(code)
            
            # 조건검색 이탈 종목 체크
            if code in self.trader.condition_excluded_stocks:
                is_holding = code in self.trader.holdings
                if not is_holding:
                    if is_first_debug:
                        self.logger.debug(f"🚫 [{code}] 조건검색 이탈 종목이므로 매수 진입 제한")
                    return False
            
            if not hasattr(self.parent, 'chart_cache') or not self.parent.chart_cache:
                return False
            
            cache_data = self.parent.chart_cache.get_cached_data(code)
            if not cache_data:
                return False
            
            tic_data = cache_data.get('tic_data', {})
            min_data = cache_data.get('min_data', {})
            
            if not tic_data or not min_data:
                return False

            if not hasattr(self.parent, 'objstg') or not self.parent.objstg:
                return False
            
            previous_close = cache_data.get('previous_close', 0)
            current_price = tic_data.get('close', [0])[-1] if tic_data.get('close') else 0
            volume = tic_data.get('volume', [0])[-1] if tic_data.get('volume') else 0

            market_data = {
                'tic_data': tic_data,
                'min_data': min_data,
                'current_price': current_price,
                'volume': volume,
                'change_rate': 0,
                'previous_close': previous_close
            }
            
            await self.parent.objstg.evaluate_strategy(code, market_data, is_buy_check_allowed=is_buy_check_allowed)
            return True
        except Exception as ex:
            self.logger.error(f"매매 판단 및 실행 실패 ({code}): {ex}", exc_info=True)
            return False
