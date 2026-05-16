import logging
import asyncio

from config_manager import EnvConfigParser
from PyQt6.QtCore import QObject, pyqtSignal


class TradingManager(QObject):
    """매매 실행 관리 매니저"""
    
    # 메시지 박스 표시를 위한 시그널 정의
    show_message_signal = pyqtSignal(str, str)
    
    def __init__(self, parent):
        super().__init__()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
        self.show_message_signal.connect(parent.show_message_box)

    def get_target_buy_count(self):
        """.env에서 최대투자 종목수 읽기"""
        try:
            config = EnvConfigParser()
            if config.has_option('BUYCOUNT', 'target_buy_count'):
                return config.getint('BUYCOUNT', 'target_buy_count')
            else:
                return 3  # 기본값 # type: ignore
        except Exception as ex:
            self.logger.error(f"target_buy_count 읽기 실패: {ex}")
            return 3  # 기본값

    def buycount_setting(self):
        """투자 종목수 설정"""
        try:
            buycount = int(self.parent.trading_tab.buycountEdit.text())
            if buycount > 0:
                # .env 파일에 저장
                config = EnvConfigParser()
                
                # BUYCOUNT 섹션이 없으면 생성
                if not config.has_section('BUYCOUNT'):
                    config.add_section('BUYCOUNT')
                
                # 값 설정
                config.set('BUYCOUNT', 'target_buy_count', str(buycount))
                
                # 파일에 저장
                with open('.env', 'w', encoding='utf-8') as configfile:
                    config.write(configfile)
                
                # 메모리에도 저장 (하위 호환성)
                if hasattr(self.parent, 'trader'):
                    self.parent.trader.buycount = buycount
                
                self.logger.info(f"✅ 최대 투자 종목수 설정 완료: {buycount}종목")
                # QMessageBox.information(self.parent, "설정 완료", f"최대 투자 종목수가 {buycount}종목으로 설정되었습니다.")
            else:
                self.logger.warning("1 이상의 숫자를 입력해주세요.")
                # QMessageBox.warning(self.parent, "입력 오류", "1 이상의 숫자를 입력해주세요.")
        except ValueError:
            self.logger.warning("올바른 숫자를 입력해주세요.")
            # QMessageBox.warning(self.parent, "입력 오류", "올바른 숫자를 입력해주세요.")
        except Exception as ex:
            self.logger.error(f"투자 종목수 설정 실패: {ex}")
            # QMessageBox.critical(self.parent, "설정 실패", f"설정 중 오류가 발생했습니다:\n{ex}")
    
    async def delete_select_item(self):
        """선택된 종목 삭제"""
        try:
            current_item = self.parent.trading_tab.monitoringBox.currentItem()
            if not current_item:
                self.logger.warning("삭제할 종목을 선택해주세요.")
                return

            item_text = current_item.text()
            code = item_text.split()[0]

            # MonitoringManager를 통해 종목 제거 (UI, 캐시, 웹소켓 구독 해제 모두 처리)
            if hasattr(self.parent, 'monitoring_manager'):
                await self.parent.monitoring_manager.remove_stock_from_monitoring(code)

        except Exception as ex:
            self.logger.error(f"종목 삭제 실패: {ex}")
    
    async def add_stock_to_list(self):
        """투자 대상 종목 리스트에 종목 추가 (API 큐를 통한 차트 데이터 수집 후 추가)"""
        try:
            stock_input = self.parent.trading_tab.stockInputEdit.text().strip()
            if not stock_input:
                self.logger.warning("종목명 또는 종목코드를 입력해주세요.")
                return
            
            # 종목코드 정규화 (6자리 숫자로 변환)
            stock_code, stock_name = await self.parent.data_manager.normalize_stock_input(stock_input)
            
            # 종목명 검색 실패 시 처리
            if stock_code is None or stock_name is None:
                self.logger.error(f"❌ 종목을 찾을 수 없습니다: {stock_input}")
                return
            
            # 이미 모니터링에 존재하는지 확인
            for i in range(self.parent.trading_tab.monitoringBox.count()):
                existing_code = self.parent.trading_tab.monitoringBox.item(i).text()
                if existing_code == stock_code:
                    self.logger.warning(f"'{stock_name}' 종목이 이미 모니터링에 존재합니다.")
                    return
            
            # 입력 필드 초기화
            self.parent.trading_tab.stockInputEdit.clear()
            
            # API 큐에 추가 (차트 데이터 수집 후 모니터링에 추가)
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                if self.parent.chart_cache.add_stock_to_api_queue(stock_code):
                    self.logger.debug(f"📋 수동 추가 종목을 API 큐에 추가: {stock_code}")
                    self.logger.debug("📋 차트 데이터 수집 완료 후 모니터링에 추가됩니다")
                else:
                    self.logger.warning(f"⚠️ API 큐 추가 실패: {stock_code}")
            else:
                self.logger.error("❌ chart_cache가 없어 종목을 추가할 수 없습니다")
            
        except Exception as ex:
            self.logger.error(f"종목 추가 실패: {ex}")
    
    def trading_mode_changed(self):
        """거래 모드 변경 이벤트 핸들러"""
        try:
            mode = "모의투자" if self.parent.trading_tab.tradingModeCombo.currentIndex() == 0 else "실제투자"
            self.logger.debug(f"거래 모드 변경: {mode}")
            
            # 키움 클라이언트의 is_mock 설정 업데이트
            if hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'kiwoom_client') and self.parent.login_handler.kiwoom_client:
                is_mock = (self.parent.trading_tab.tradingModeCombo.currentIndex() == 0)
                self.parent.login_handler.kiwoom_client.is_mock = is_mock
                self.logger.debug(f"키움 클라이언트 모의투자 설정 업데이트: {is_mock}")
            
            # 연결된 상태라면 재연결 안내 (로그로만 표시)
            if hasattr(self.parent, 'trader') and self.parent.trader and self.parent.trader.client and self.parent.trader.client.is_connected:
                self.logger.debug(f"거래 모드가 {mode}로 변경되었습니다. 새로운 설정을 적용하려면 API를 재연결해주세요.")
                
        except Exception as ex:
            self.logger.error(f"거래 모드 변경 실패: {ex}")
    
    async def sell_all_item(self, is_auto=False):
        """전체 매도 (키움 REST API 기반) (비동기)
        
        Args:
            is_auto: True면 자동 청산, False면 수동 전체 매도
        """
        # 다른 비동기 작업과의 충돌을 막기 위해 락을 사용합니다.
        if self.parent.trading_lock.locked():
            self.logger.warning("⚠️ 다른 수동 매매 작업이 이미 진행 중입니다.")
            return
        # 자동매매 타이머 일시 중지
        autotrader = getattr(self.parent, 'autotrader', None)
        chart_cache = getattr(self.parent, 'chart_cache', None)
        if autotrader:
            autotrader.stop_auto_trading()
        if chart_cache and chart_cache.update_timer:
            chart_cache.update_timer.stop()
        
        # 로그 메시지 구분
        if is_auto:
            self.logger.info("자동 청산 - 모든 주기적 타이머 일시 중지")
        else:
            self.logger.info("수동 전체 매도 시작 - 모든 주기적 타이머 일시 중지")
        try:
            async with self.parent.trading_lock:
                if self.parent.trading_tab.boughtBox.count() == 0:
                    self.logger.warning("매도할 종목이 없습니다.")
                    # 타이머 재시작
                    self._restart_timers_after_manual_trade(autotrader, chart_cache)
                    return

                # 로그 메시지 구분
                if is_auto:
                    self.logger.info("🔄 자동 청산 매도 시작")
                else:
                    self.logger.info("🔄 전체 매도 시작")
                
                # 보유 종목 목록 생성
                sell_items = []
                for i in range(self.parent.trading_tab.boughtBox.count()):
                    item = self.parent.trading_tab.boughtBox.item(i)
                    item_text = item.text()
                    code = item_text.split()[0]
                    sell_items.append(code)
                
                # 각 종목에 대해 매도 주문 실행
                success_count = 0
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
                                self.logger.debug(f"💰 웹소켓 잔고: {code} {quantity}주")
                        
                        # 2차: 웹소켓 데이터가 없거나 수량이 0이면 REST API로 조회
                        if quantity <= 0:
                            try:
                                if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                                    balance_result = await self.parent.login_handler.kiwoom_client.get_acnt_balance()
                                    if balance_result:
                                        holdings = balance_result.get('stk_acnt_evlt_prst', balance_result.get('output1', []))
                                        for stock in holdings:
                                            raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                                            stock_code = self.parent.data_manager.normalize_stock_code(raw_code)
                                            if stock_code == code:
                                                quantity = self.parent.data_manager.safe_int(stock.get('rmnd_qty', stock.get('hldg_qty', 0)))
                                                self.logger.debug(f"📡 REST API 잔고: {code} {quantity}주")
                                                break
                            except Exception as api_ex:
                                self.logger.error(f"❌ REST API 잔고 조회 실패: {api_ex}")
                        
                        # 수량 확인
                        if quantity <= 0:
                            self.logger.warning(f"⚠️ {code} 보유 수량 없음 - 건너뜀")
                            continue

                        # 이미 매도 주문 진행 중인지 확인 (중복 매도 방지)
                        if hasattr(self.parent, 'trader') and self.parent.trader and code in self.parent.trader.pending_sell_orders:
                            self.logger.info(f"⏳ {code} 이미 매도 주문이 진행 중이므로 자동 청산에서 건너뜁니다.")
                            continue
                        
                        # 매도 주문 실행 (재시도 로직 포함)
                        if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                            max_retries = 3
                            retry_delay = 1.0  # 초기 대기 시간
                            success = False
                            
                            for attempt in range(max_retries):
                                try:
                                    success = await self.parent.login_handler.kiwoom_client.place_sell_order(code, quantity, 0, "market")
                                    
                                    if success:
                                        success_count += 1
                                        if is_auto:
                                            self.logger.info(f"✅ 자동 청산 성공: {code} {quantity}주")
                                        else:
                                            self.logger.info(f"✅ 전체 매도 성공: {code} {quantity}주")
                                        break  # 성공하면 재시도 중단
                                    else:
                                        self.logger.warning(f"⚠️ 매도 주문 실패 (시도 {attempt + 1}/{max_retries}): {code}")
                                        
                                except Exception as order_ex:
                                    error_msg = str(order_ex)
                                    
                                    # HTTP 429 (Rate Limit) 오류 감지
                                    if "429" in error_msg or "Too Many Requests" in error_msg:
                                        if attempt < max_retries - 1:
                                            self.logger.warning(f"⚠️ API Rate Limit 초과 - {retry_delay}초 후 재시도 ({attempt + 1}/{max_retries}): {code}")
                                            await asyncio.sleep(retry_delay)
                                            retry_delay *= 2  # 지수 백오프
                                            continue
                                        else:
                                            self.logger.error(f"❌ API Rate Limit 초과 - 최대 재시도 횟수 도달: {code}")
                                    else:
                                        self.logger.error(f"❌ 매도 주문 오류: {code} - {error_msg}")
                                        break
                            
                            if not success:
                                if is_auto:
                                    self.logger.error(f"❌ 자동 청산 실패: {code}")
                                else:
                                    self.logger.error(f"❌ 전체 매도 실패: {code}")
                            
                            # API 요청 제한을 피하기 위해 종목 간 지연 추가
                            if len(sell_items) > 1:
                                await asyncio.sleep(0.8)  # 0.5초 -> 0.8초로 증가
                                
                    except Exception as item_ex:
                        self.logger.error(f"❌ {code} 매도 중 오류: {item_ex}")
                
                # 결과 로그
                if success_count > 0:
                    if is_auto:
                        self.logger.info(f"✅ 자동 청산 완료: {success_count}개 종목 매도")
                    else:
                        self.logger.info(f"✅ 전체 매도 완료: {success_count}개 종목")
                else:
                    if is_auto:
                        self.logger.error("❌ 자동 청산 실패")
                    else:
                        self.logger.error("❌ 전체 매도 실패")
        except Exception as ex:
            self.logger.error(f"전체 매도 작업 중 오류 발생: {ex}", exc_info=True)
            # QMessageBox.critical(self.parent, "전체 매도 오류", f"전체 매도 중 오류가 발생했습니다: {ex}")
        finally:
            # 자동매매 타이머 다시 시작
            await self._restart_timers_after_manual_trade(autotrader, chart_cache)
    
    async def sell_item(self):
        """종목 매도 - 보유수량 전량 매도 (키움 REST API 기반) (비동기)"""
        if self.parent.trading_lock.locked():
            self.logger.warning("⚠️ 다른 수동 매매 작업이 이미 진행 중입니다.")
            return
        # 자동매매 타이머 일시 중지
        autotrader = getattr(self.parent, 'autotrader', None)
        chart_cache = getattr(self.parent, 'chart_cache', None)
        if autotrader:
            autotrader.stop_auto_trading()
        if chart_cache and chart_cache.update_timer:
            chart_cache.update_timer.stop()
        self.logger.debug("수동 매도 시작 - 모든 주기적 타이머 일시 중지")
        try:
            async with self.parent.trading_lock:
                current_item = self.parent.trading_tab.boughtBox.currentItem()
                if not current_item:
                    self.logger.warning("매도할 종목을 선택해주세요.")
                    # QMessageBox.warning(self.parent, "선택 오류", "매도할 종목을 선택해주세요.")
                    # 타이머 재시작
                    await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                    return

                item_text = current_item.text()
                code = item_text.split()[0]
                
                self.logger.debug(f"매도 요청: {code}")
                
                quantity = 0
                
                # 1차: REST API로 주문가능수량 조회
                try:
                    if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                        balance_result = await self.parent.login_handler.kiwoom_client.get_acnt_balance()
                        if balance_result:
                            holdings = balance_result.get('stk_acnt_evlt_prst', balance_result.get('output1', []))
                            for stock in holdings:
                                raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                                stock_code = self.parent.data_manager.normalize_stock_code(raw_code)
                                if stock_code == code:
                                    quantity = self.parent.data_manager.safe_int(stock.get('rmnd_qty', 0))
                                    self.logger.debug(f"✅ REST API로 주문가능수량 조회 성공: {code} {quantity}주")
                                    break
                except Exception as api_ex:
                    self.logger.error(f"❌ REST API 잔고 조회 실패: {api_ex}")

                # 2차: REST API 조회 실패 또는 수량 0일 때 웹소켓 데이터로 재확인
                if quantity <= 0:
                    if (hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'websocket_client')):
                        ws_balance_data = self.parent.login_handler.websocket_client.balance_data
                        if ws_balance_data and code in ws_balance_data:
                            quantity = ws_balance_data[code].get('order_available_qty', 0)
                            self.logger.info(f"💰 웹소켓 잔고 조회 (Fallback): {code} 주문가능수량 {quantity}주")
                
                if quantity <= 0:
                    self.logger.warning(f"⚠️ 보유 수량 없음: {code}")
                    # QMessageBox.warning(self.parent, "매도 불가", f"{code} 보유 수량이 없습니다.\n웹소켓과 REST API 모두 확인했습니다.")
                    await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                    return                
                
                if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                    success = await self.parent.login_handler.kiwoom_client.place_sell_order(code, quantity, 0, "market")
                    if success:
                        self.logger.debug(f"✅ 매도 주문 성공: {code} {quantity}주 전량 매도")
                    else:
                        self.logger.error(f"❌ 매도 주문 실패: {code}")
                        # QMessageBox.warning(self.parent, "매도 실패", f"{code} 매도 주문이 실패했습니다.")
                else:
                    self.logger.error("키움 클라이언트가 초기화되지 않았습니다")
                    # QMessageBox.warning(self.parent, "오류", "키움 클라이언트가 초기화되지 않았습니다.")
        except Exception as ex:
            self.logger.error(f"매도 작업 중 오류 발생: {ex}", exc_info=True)
            # QMessageBox.critical(self.parent, "매도 오류", f"매도 중 오류가 발생했습니다: {ex}")
        finally:
            # 자동매매 타이머 다시 시작
            await self._restart_timers_after_manual_trade(autotrader, chart_cache)
    
    async def buy_item(self):
        """종목 매입 - 자동 매입가능수량 계산 (키움 REST API 기반) (비동기)"""
        if self.parent.trading_lock.locked():
            self.logger.warning("⚠️ 다른 수동 매매 작업이 이미 진행 중입니다.")
            return
        # 자동매매 타이머 일시 중지
        autotrader = getattr(self.parent, 'autotrader', None)
        chart_cache = getattr(self.parent, 'chart_cache', None)
        if autotrader:
            autotrader.stop_auto_trading()
        if chart_cache and chart_cache.update_timer:
            chart_cache.update_timer.stop()
        self.logger.debug("수동 매수 시작 - 모든 주기적 타이머 일시 중지")
        try:
            async with self.parent.trading_lock:
                current_item = self.parent.trading_tab.monitoringBox.currentItem()
                if not current_item:
                    self.logger.warning("매입할 종목을 선택해주세요.")
                    # self.show_message_signal.emit("선택 오류", "매입할 종목을 선택해주세요.") # QMessageBox 직접 호출 대신 시그널 사용
                    await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                    return

                # "종목코드 - 종목명" 또는 "종목코드" 형식에서 종목코드만 정확히 추출
                item_text = current_item.text()
                code = item_text.split()[0]
                
                if hasattr(self.parent, 'boughtBox'):
                    for i in range(self.parent.boughtBox.count()):
                        bought_item_text = self.parent.trading_tab.boughtBox.item(i).text()
                        # "종목코드 - 종목명" 또는 "종목코드" 형식에서 종목코드만 추출하여 비교
                        bought_code = bought_item_text.split()[0]
                        
                        if bought_code == code:
                            self.logger.info(f"⚠️ 매수 주문 취소: {code}는 이미 보유 중인 종목입니다.")
                            # self.show_message_signal.emit("매수 불가", f"{code}는 이미 보유 중인 종목입니다.") # QMessageBox 직접 호출 대신 시그널 사용
                            await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                            return
                
                quantity = 0
                try:
                    if not hasattr(self.parent, 'trader') or not self.parent.trader:
                        self.logger.error("⚠️ trader가 초기화되지 않았습니다 (API 연결이 필요합니다)")
                        # self.show_message_signal.emit("오류", "API에 먼저 연결해주세요.") # QMessageBox 직접 호출 대신 시그널 사용
                        await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                        return
                
                    available_cash = await self.parent.trader.get_available_cash()
                    if available_cash <= 0:
                        self.logger.warning(f"⚠️ 매수 주문 취소: 투자가능금액 부족 ({available_cash:,.0f}원)")
                        # self.show_message_signal.emit("매수 불가", f"투자가능금액이 부족합니다.\n현재: {available_cash:,.0f}원") # QMessageBox 직접 호출 대신 시그널 사용
                        await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                        return
                    
                    available_buy_count = self.parent.login_handler.get_available_buy_count()
                    if available_buy_count <= 0:
                        self.logger.warning(f"⚠️ 매수 주문 취소: 최대 보유 종목 수 도달")
                        # self.show_message_signal.emit("매수 불가", "최대 보유 종목 수에 도달했습니다.") # QMessageBox 직접 호출 대신 시그널 사용
                        await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                        return
                    
                    current_price = 0
                    price_source = ""
                    
                    # 1순위: ChartDataCache에서 현재가 조회 (올바른 경로로 수정)
                    if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                        cached_data = self.parent.chart_cache.get_cached_data(code)
                        if cached_data and cached_data.get('tic_data'):
                            tic_data = cached_data['tic_data']
                            if tic_data.get('close') and len(tic_data['close']) > 0:
                                current_price = float(tic_data['close'][-1])
                                price_source = "캐시"
                    
                    # 2순위: 캐시 조회 실패 시 REST API로 현재가 조회
                    if current_price <= 0:
                        try:
                            current_price = await self.parent.trader.get_current_price(code)
                            if current_price > 0: price_source = "API"
                        except Exception as price_ex:
                            self.logger.debug(f"현재가 조회 실패: {price_ex}")

                    if current_price <= 0:
                        self.logger.error(f"❌ 현재가 조회에 실패하여 매수 주문을 취소합니다: {code}")
                        # self.show_message_signal.emit("매수 불가", f"{code}의 현재가 조회에 실패했습니다.")
                        await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                        return
                    
                    budget_per_stock = available_cash // available_buy_count
                    quantity = int(budget_per_stock / current_price)
                    if quantity <= 0: quantity = 1
                    
                    self.logger.debug(f"🛒 {code} 매수: {quantity}주 @ 시장가 (예산 {budget_per_stock:,.0f}원, 현재가 {current_price:,.0f}원/{price_source})")
                    
                except Exception as calc_ex:
                    self.logger.error(f"❌ 매수 수량 계산 실패: {calc_ex}")
                    # self.show_message_signal.emit("오류", f"매수 수량 계산 중 오류가 발생했습니다:\n{calc_ex}") # QMessageBox 직접 호출 대신 시그널 사용
                    await self._restart_timers_after_manual_trade(autotrader, chart_cache)
                    return
                
                if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                    success = await self.parent.login_handler.kiwoom_client.place_buy_order(code, quantity, 0, "market")
                    if not success:
                        self.logger.error(f"❌ 매수 주문 실패: {code}")
                        # self.show_message_signal.emit("매수 실패", f"{code} 매수 주문이 실패했습니다.") # QMessageBox 직접 호출 대신 시그널 사용
                else:
                    self.logger.error("키움 클라이언트가 초기화되지 않았습니다")
                    # self.show_message_signal.emit("오류", "키움 클라이언트가 초기화되지 않았습니다.") # QMessageBox 직접 호출 대신 시그널 사용
        except Exception as ex:
            self.logger.error(f"매입 작업 중 오류 발생: {ex}", exc_info=True)
            # self.show_message_signal.emit("매입 오류", f"매입 중 오류가 발생했습니다:\n{ex}") # QMessageBox 직접 호출 대신 시그널 사용
        finally:
            # 자동매매 타이머 다시 시작
            await self._restart_timers_after_manual_trade(autotrader, chart_cache)
    
    async def _restart_timers_after_manual_trade(self, autotrader, chart_cache):
        """수동 매매 후 타이머들을 재시작하는 헬퍼 함수"""
        await asyncio.sleep(1)  # 1초 비동기 대기
        if autotrader:
            autotrader.start_auto_trading()
        if chart_cache:
            chart_cache.start()
        self.logger.debug("수동 매매 완료 - 모든 주기적 타이머 다시 시작")

