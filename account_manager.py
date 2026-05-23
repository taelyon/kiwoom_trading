import logging
from datetime import datetime


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
                    
                    # 웹 대시보드 및 트레이더에서 실시간 파악할 수 있도록 캐시 및 balance_data 업데이트
                    if hasattr(self.parent, 'trader') and self.parent.trader:
                        self.parent.trader._cash_cache = entr_amount
                        if not hasattr(self.parent.trader, 'balance_data') or self.parent.trader.balance_data is None:
                            self.parent.trader.balance_data = {}
                        self.parent.trader.balance_data['available_cash'] = entr_amount
            except Exception as deposit_ex:
                self.logger.error(f"❌ 예수금상세현황 조회 실패: {deposit_ex}")

            # 2. 계좌평가잔고내역 조회 (kt00018)
            try:
                self.logger.debug("🔍 계좌평가잔고내역 조회 중...")
                eval_status = await self.parent.trader.client.get_account_evaluation_status()
                if eval_status:
                    total_purchase_amount = self.parent.data_manager.safe_int(eval_status.get('tot_pur_amt', 0))
                    total_eval_pl_amount = self.parent.data_manager.safe_int(eval_status.get('tot_evlt_pl', 0))
                    total_profit_rate = self.parent.data_manager.safe_float(eval_status.get('tot_prft_rt', 0.0))

                    self.logger.info(f"총매입금액: {total_purchase_amount:,}원")
                    self.logger.info(f"총평가손익금액: {total_eval_pl_amount:+,}원")
                    self.logger.info(f"총수익률: {total_profit_rate:.2f}%")

                    # 종목별 평가손익 및 수익률 로깅
                    stock_list = eval_status.get('acnt_evlt_remn_indv_tot', [])
                    if stock_list:
                        for stock in stock_list:
                            stock_name = stock.get('stk_nm', 'N/A')
                            eval_profit = self.parent.data_manager.safe_int(stock.get('evltv_prft', 0))
                            profit_rate = self.parent.data_manager.safe_float(stock.get('prft_rt', 0.0))
                            self.logger.info(f"- {stock_name}: 평가손익 {eval_profit:+,}원, 수익률 {profit_rate:.2f}%")
                else:
                    self.logger.warning("⚠️ 계좌평가잔고내역 조회 실패")
            except Exception as eval_ex:
                self.logger.error(f"❌ 계좌평가잔고내역 조회 중 오류: {eval_ex}", exc_info=True)

            # 3. REST API 잔고조회
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
                    
                    # 최근 매도된 종목이면 건너뛰기 (UI 재출현 방지)
                    if hasattr(parent, 'trader') and parent.trader and parent.trader.is_recently_sold(stock_code):
                        self.logger.debug(f"🚫 {stock_code} 최근 매도된 종목이므로 REST API 잔고 반영 건너뜀")
                        continue

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

