import logging
import asyncio
from config_manager import EnvConfigParser
import json
import traceback
import pandas as pd
from datetime import datetime
from utils import CallbackSignal

import strategy_utils

# ==================== 키움 전략 클래스 ====================
class KiwoomStrategy:
    """키움 REST API 기반 전략 클래스 (Pure Python)"""
    
    def __init__(self, trader, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.trader = trader
        self.client = trader.client
        self.db_manager = trader.db_manager
        self.parent = parent

        # 시그널 정의 (콜백 기반)
        self.signal_strategy_result = CallbackSignal()  # code, action, data
        self.clear_signal = CallbackSignal()

        # 종목별 매수 신호 생성 동시성 제어를 위한 Lock
        self._buy_signal_locks: dict[str, asyncio.Lock] = {}
        
        # PyQt6에서는 QTextCursor 메타타입 등록이 불필요함
        
        # 전략 설정 로드
        self.load_strategy_config()
            
    def load_strategy_config(self):
        """전략 설정 로드"""
        try:
            config = EnvConfigParser()
            
            # 전략별 설정 로드 - [STRATEGIES] 섹션 기반으로 동적 로드
            self.strategy_config = {}
            available_strategies = []
            if config.has_section('STRATEGIES'):
                for key, strategy_name in config.items('STRATEGIES'):
                    if key.startswith('stg_') and key != 'stg_integrated':
                        available_strategies.append(strategy_name)
                        # 해당 전략명과 일치하는 섹션이 있으면 로드
                        if config.has_section(strategy_name):
                            self.strategy_config[strategy_name] = dict(config.items(strategy_name)) # type: ignore
                            self.logger.debug(f"✅ 전략 설정 로드: {strategy_name}")
            
            # 현재 전략 로드
            last_stg = config.get('SETTINGS', 'last_strategy', fallback=None)
            if last_stg and last_stg in available_strategies:
                self.current_strategy = last_stg
            else:
                self.current_strategy = available_strategies[0] if available_strategies else None
            
            self.logger.debug(f"전략 설정 로드 완료: {self.current_strategy}")
            
        except Exception as ex:
            self.logger.error(f"전략 설정 로드 실패: {ex}", exc_info=True)

    def _log_indicator_values(self, code, condition, safe_locals):
        """조건식에 사용된 지표 값 로그 출력"""
        try:
            import re
            
            # 조건식에서 변수명 추출 (알파벳, 숫자, 언더스코어로 구성된 단어)
            # 함수 호출 등은 제외하기 위해 간단한 단어 매칭 사용
            tokens = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', condition)
            
            # safe_locals에 있는 키만 필터링 (builtins 제외)
            used_vars = set(tokens) & set(safe_locals.keys())
            if '__builtins__' in used_vars:
                used_vars.remove('__builtins__')
            
            # 주요 지표 값 로그 출력
            log_messages = []
            for var_name in sorted(used_vars):
                value = safe_locals[var_name]
                
                # numpy array나 list인 경우 마지막 값(현재 값) 출력
                if hasattr(value, '__len__') and not isinstance(value, str):
                     if len(value) > 0:
                        try:
                            last_val = value[-1]
                            if isinstance(last_val, (int, float)):
                                log_messages.append(f"{var_name}[-1]={last_val:.2f}")
                            else:
                                log_messages.append(f"{var_name}[-1]={last_val}")
                        except Exception:
                            log_messages.append(f"{var_name}={value}")
                     else:
                        log_messages.append(f"{var_name}=[]")
                elif isinstance(value, (int, float)):
                    log_messages.append(f"{var_name}={value:.2f}")
                else:
                    log_messages.append(f"{var_name}={value}")
            
            if log_messages:
                self.logger.info(f"📊 [{code}] 조건 지표 값: {', '.join(log_messages)}")
                
        except Exception as e:
            self.logger.warning(f"지표 값 로깅 중 오류: {e}")
    
    async def evaluate_strategy(self, code, market_data, is_buy_check_allowed=False):
        """전략 평가 및 실행 (비동기)"""
        try:
            # 디버그 로그: 최초 1회만 출력 (종목별)
            if not hasattr(self, '_eval_debug_codes'):
                self._eval_debug_codes = set()
            
            is_first_eval = code not in self._eval_debug_codes
            if is_first_eval:
                self.logger.debug(f"📊 [{code}] 전략 평가 시작 (전략: {self.current_strategy})")
                self._eval_debug_codes.add(code)
            
            # 현재 전략에 따른 매수/매도 신호 평가
            # 1. 기본 전략이 선택된 경우: 모든 종목에 기본 전략 적용
            # 2. 조건검색으로 찾은 종목: 해당 조건검색의 전략 사용
            # 3. 그 외: 현재 선택된 전략 사용
            
            stock_condition_map = self.parent.stock_condition_map if hasattr(self.parent, 'stock_condition_map') else {}
            
            # UI에서 선택된 전략
            selected_strategy_name = self.current_strategy
            
            # 실제 적용할 전략 결정
            # 1순위: 조건검색으로 포착된 종목은 해당 조건검색식의 전략을 사용
            if code in stock_condition_map:
                effective_strategy_name = stock_condition_map[code]
                if is_first_eval:
                    self.logger.debug(f"📍 [{code}] 조건검색 전략 사용: {effective_strategy_name} (UI 선택: {selected_strategy_name})")
            # 2순위: 그 외의 경우(수동 추가 등)는 UI에서 선택된 전략을 사용
            else:
                effective_strategy_name = selected_strategy_name
                if is_first_eval:
                    self.logger.debug(f"📍 [{code}] UI 선택 전략 사용: {effective_strategy_name}")
            
            if effective_strategy_name not in self.strategy_config:
                if is_first_eval:
                    self.logger.warning(f"⚠️ [{code}] 전략 '{effective_strategy_name}'이 설정에 없음 - 평가를 진행하지 않습니다.")
                return None, None
            elif is_first_eval:
                self.logger.debug(f"✅ [{code}] 전략 설정 확인됨: {effective_strategy_name}")

            # 매도 주문이 진행 중인 경우 전략 평가 건너뛰기 (중복 주문 방지)
            if code in self.trader.pending_sell_orders:
                # 디버그 로그는 최초 1회만 출력 (너무 빈번함)
                if not hasattr(self, '_pending_order_log_codes'):
                    self._pending_order_log_codes = set()
                
                if code not in self._pending_order_log_codes:
                    self.logger.debug(f"⏳ [{code}] 매도 주문 API 요청 중이므로 전략 평가를 건너뜁니다.")
                    self._pending_order_log_codes.add(code)
                return

            # [추가] 이미 매도 주문이 체결 대기 중인 경우 전략 평가 건너뛰기 (중복 매도 방지)
            # sell_order_details에 해당 종목의 주문이 있다면 아직 체결되지 않은 상태
            active_sell_orders = [ord_no for ord_no, details in self.trader.sell_order_details.items() if details.get('code') == code]
            if active_sell_orders:
                if not hasattr(self, '_active_order_log_codes'):
                    self._active_order_log_codes = set()
                
                # 로그 중복 방지 (5초에 한 번만 출력하거나 상태 변경 시 출력)
                current_time = datetime.now().timestamp()
                last_log_time = getattr(self, f'_last_log_time_{code}', 0)
                
                if current_time - last_log_time > 5:
                    self.logger.warning(f"⏳ [{code}] 매도 체결 대기 중인 주문이 있어 전략 평가를 건너뜁니다. (주문번호: {active_sell_orders})")
                    setattr(self, f'_last_log_time_{code}', current_time)
                
                return
            
            # 일시적 차단 목록 확인 (주문가능수량 0 등으로 인한 무한 루프 방지)
            if hasattr(self, '_temp_blocked_codes') and code in self._temp_blocked_codes:
                blocked_until = self._temp_blocked_codes[code]
                if datetime.now().timestamp() < blocked_until:
                    # 차단 기간 중에는 평가 건너뜀 (로그 생략하여 노이즈 감소)
                    return
                else:
                    # 차단 기간 만료 시 목록에서 제거
                    del self._temp_blocked_codes[code]
                    self.logger.debug(f"🔓 [{code}] 전략 평가 일시 차단 해제")
            
            # 매수 신호 평가 (is_buy_check_allowed가 True일 때만 실행)
            if is_buy_check_allowed:
                # [추가] 글로벌 계좌 서킷 브레이커 발동 중이면 신규 매수 전면 차단
                is_circuit_breaker_active = False
                if hasattr(self.parent, 'autotrader') and self.parent.autotrader:
                    is_circuit_breaker_active = getattr(self.parent.autotrader, 'circuit_breaker_triggered', False)
                    
                if is_circuit_breaker_active:
                    if is_first_eval:
                        self.logger.warning(f"🚨 [{code}] 서킷 브레이커 발동 중으로 신규 매수 진입이 전면 차단되었습니다.")
                else:
                    buy_signals = await self.get_buy_signals(code, market_data, effective_strategy_name)
                    if buy_signals:
                        self.logger.debug(f"📈 [{code}] 매수 신호 {len(buy_signals)}개 발견")
                        await self.execute_buy_signals(code, buy_signals)
                    elif is_first_eval:
                        self.logger.debug(f"ℹ️ [{code}] 매수 조건 미충족")
            
            # 매도 신호 평가 (보유 종목인 경우에만)
            portfolio = self.trader.get_portfolio_status()
            if code in portfolio['holdings']:
                if is_first_eval:
                    self.logger.debug(f"🔎 [{code}] 보유 종목 - 매도 평가 진행")
                sell_signals = await self.get_sell_signals(code, market_data, effective_strategy_name)
                if sell_signals:
                    self.logger.debug(f"📉 [{code}] 매도 신호 {len(sell_signals)}개 발견")
                    await self.execute_sell_signals(code, sell_signals)
            else:
                # 보유 종목이 아닌 경우 디버그 로그 (최초 1회만)
                if is_first_eval:
                    # 웹소켓 balance_data 확인
                    ws_has_stock = False
                    try:
                        if (hasattr(self.parent, 'login_handler') and self.parent.login_handler and
                            hasattr(self.parent.login_handler, 'websocket_client') and self.parent.login_handler.websocket_client and
                            hasattr(self.parent.login_handler.websocket_client, 'balance_data')):
                            ws_balance_data = self.parent.login_handler.websocket_client.balance_data
                            if ws_balance_data and code in ws_balance_data:
                                ws_quantity = ws_balance_data[code].get('quantity', 0)
                                if ws_quantity > 0:
                                    ws_has_stock = True
                                    self.logger.warning(f"⚠️ [{code}] 웹소켓에는 보유 중이지만 self.holdings에 없음 (웹소켓 수량: {ws_quantity}주)")
                                    self.logger.warning(f"get_portfolio_status 동기화 필요 - holdings: {list(portfolio['holdings'].keys())}, 웹소켓: {list(ws_balance_data.keys())}")
                    except Exception as ws_check_ex:
                        self.logger.debug(f"웹소켓 체크 중 오류: {ws_check_ex}", exc_info=True)
                    
                    if not ws_has_stock:
                        self.logger.debug(f"ℹ️ [{code}] 보유 종목 아님 - 매도 평가 건너뜀")
            
            # 급등주 전략일 경우 모멘텀 상실 체크 (보유 여부 상관없이 체크)
            if effective_strategy_name == "급등주":
                await self.check_momentum_loss(code, market_data)
                    
        except Exception as ex:
            self.logger.error(f"전략 평가 실패 ({code}): {ex}", exc_info=True)

    async def check_momentum_loss(self, code, market_data):
        """급등주 모멘텀 상실 여부 확인 및 처리"""
        try:
            # 이미 블랙리스트에 있다면 중단 (중복 처리 방지)
            if self.trader.is_blacklisted(code):
                return

            # 1. 고점 대비 하락률 체크
            # 현재가
            current_price = market_data.get('current_price', 0)
            if current_price == 0:
                return

            # 당일 고가 (틱 데이터나 분봉 데이터에서 추출 필요)
            # 여기서는 간단히 틱 데이터의 고가를 사용하거나, market_data에 고가 정보가 없다면 계산
            tic_data = market_data.get('tic_data', {})
            high_price = 0
            
            # 틱 데이터에서 고가 찾기 (최근 데이터 기준)
            if tic_data and 'close' in tic_data:
                # 최근 N개의 틱 데이터 중 최고가 (또는 전체 틱 중 최고가)
                # 데이터가 많으면 전체를 다 도는 것은 비효율적일 수 있으나, 
                # 여기서는 캐시된 데이터 범위 내에서의 고가를 사용
                high_price = max(tic_data['close'])
            
            # 분봉 데이터가 있다면 분봉 고가도 고려
            min_data = market_data.get('min_data', {})
            if min_data and 'high' in min_data:
                min_high = max(min_data['high'])
                high_price = max(high_price, min_high)
            
            if high_price > 0:
                drop_rate = (current_price - high_price) / high_price * 100
                
                # 고점 대비 -5% 이상 하락 시 모멘텀 상실로 간주
                if drop_rate <= -5.0:
                    self.logger.info(f"📉 [{code}] 급등주 모멘텀 상실 감지 (고점 대비 {drop_rate:.2f}%)")
                    
                    # 블랙리스트 추가
                    if hasattr(self.trader, 'add_to_blacklist'):
                        self.trader.add_to_blacklist(code)
                        self.logger.info(f"🚫 [{code}] 블랙리스트 추가 완료 (사유: 모멘텀 상실)")
                    
                    # 모니터링 제거 요청
                    if hasattr(self.parent, 'remove_monitoring_stock'):
                        self.parent.remove_monitoring_stock(code)
                        self.logger.debug(f"🗑️ [{code}] 모니터링 목록에서 제거됨")

        except Exception as ex:
            self.logger.error(f"모멘텀 체크 중 오류 ({code}): {ex}", exc_info=True)
            
    
    async def get_buy_signals(self, code, market_data, strategy_name):
        """매수 신호 생성 - strategy_utils를 사용한 기술적 지표 기반 평가"""
        try:
            # 종목별 Lock 가져오기 또는 생성
            if code not in self._buy_signal_locks:
                self._buy_signal_locks[code] = asyncio.Lock()
            lock = self._buy_signal_locks[code]

            async with lock:
                signals = []
                
                # 디버그 로그: 최초 1회만 출력 (종목별)
                if not hasattr(self, '_buy_signal_debug_codes'):
                    self._buy_signal_debug_codes = set()
                
                is_first_check = code not in self._buy_signal_debug_codes
                if is_first_check:
                    self.logger.debug(f"🔍 [{code}] 매수 신호 검사 시작")
                    self._buy_signal_debug_codes.add(code)
                
                # 포트폴리오 상태 확인
                portfolio = self.trader.get_portfolio_status()
                if portfolio['total_holdings'] >= portfolio['max_holdings']:
                    if is_first_check:
                        self.logger.debug(f"⚠️ [{code}] 매수 불가: 보유 종목 수 한도 도달 ({portfolio['total_holdings']}/{portfolio['max_holdings']})")
                    return signals
                
                # 이미 보유 중인 종목인지 확인
                if code in portfolio['holdings']:
                    if is_first_check:
                        self.logger.debug(f"⚠️ [{code}] 매수 불가: 이미 보유 중")
                    return signals

                # 당일 매수 금지(Blacklist) 종목인지 확인 - 사용자 요청으로 제거 (재매수 허용)
                # if self.trader.is_blacklisted(code):
                #     if is_first_check:
                #         self.logger.debug(f"🚫 [{code}] 매수 불가: 당일 매수 금지 목록(Blacklist)에 포함됨")
                #     return signals

                # 재매수 대기 시간(Cooldown) 체크 (10분 = 600초)
                if hasattr(self.trader, 'last_sell_times') and code in self.trader.last_sell_times:
                    elapsed_since_sell = (datetime.now() - self.trader.last_sell_times[code]).total_seconds()
                    if elapsed_since_sell < 600:  # 10분 이내 재매수 금지
                        if is_first_check:
                            self.logger.debug(f"⏳ [{code}] 재매수 대기 중: 매도 후 {int(elapsed_since_sell)}초 경과 (10분 제한)")
                        return signals

                # '매수 주문 진행 중'인 종목은 매수 신호 생성 건너뛰기 (중복 주문 방지 강화)
                if hasattr(self.trader, 'pending_buy_orders') and code in self.trader.pending_buy_orders:
                    if is_first_check:
                        self.logger.debug(f"⏳ [{code}] 매수 주문이 이미 진행 중이므로 신호 생성을 건너뜁니다.")
                    return signals

                # '매수 주문 진행 중'인 종목은 매수 신호 생성 건너뛰기
                if code in self.trader.pending_buy_orders:
                    if is_first_check:
                        self.logger.debug(f"⏳ [{code}] 매수 주문이 이미 진행 중이므로 신호 생성을 건너뜁니다.")
                    return signals
                
                # 차트 데이터 가져오기 (틱/분봉) - chart_cache에서 직접 가져오기
                tic_chart_data = pd.DataFrame()
                min_chart_data = pd.DataFrame()
                realtime_metrics = {}
                if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                    cache_data = self.parent.chart_cache.get_cached_data(code)
                    if cache_data:
                        tic_data = cache_data.get('tic_data', {})
                        min_data = cache_data.get('min_data', {})
                        previous_close = cache_data.get('previous_close', 0)
                        current_open = cache_data.get('current_open', 0)
                        realtime_metrics = cache_data.get('realtime_metrics', {})
                        
                        # [추가] WebSocket에서 실시간 시장 지수 등락률 가져오기
                        ws_client = None
                        if hasattr(self, 'trader') and self.trader and hasattr(self.trader, 'ws_client'):
                            ws_client = self.trader.ws_client
                        elif hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'login_handler'):
                            ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
                            
                        if ws_client and hasattr(ws_client, 'market_indices'):
                            realtime_metrics['kospi_change'] = ws_client.market_indices.get('kospi_change', 0.0)
                            realtime_metrics['kosdaq_change'] = ws_client.market_indices.get('kosdaq_change', 0.0)

                        if tic_data and len(tic_data.get('close', [])) > 0:
                            try:
                                # DataFrame 생성 시 모든 배열 길이가 동일해야 함. 가장 짧은 길이로 맞춤
                                min_len = min(len(v) for k, v in tic_data.items() if isinstance(v, list))
                                trimmed_tic_data = {k: v[:min_len] for k, v in tic_data.items() if isinstance(v, list)}
                                tic_chart_data = pd.DataFrame(trimmed_tic_data).dropna().reset_index(drop=True)
                            except Exception as ex:
                                if is_first_check:
                                    self.logger.warning(f"차트 데이터 변환 실패 ({code}): {ex}", exc_info=True)
                                tic_chart_data = pd.DataFrame()

                        if min_data and len(min_data.get('close', [])) > 0:
                            try:
                                min_chart_data = pd.DataFrame(min_data).dropna().reset_index(drop=True)
                            except Exception as ex:
                                if is_first_check:
                                    self.logger.warning(f"분봉 차트 데이터 변환 실패 ({code}): {ex}", exc_info=True)
                                min_chart_data = pd.DataFrame()

                        if is_first_check:
                            self.logger.debug(f"✅ [{code}] 차트 데이터 준비 완료: {len(tic_chart_data)}개 틱, {len(min_chart_data)}개 분봉")
                    else:
                        if is_first_check:
                            self.logger.warning(f"⚠️ [{code}] cache_data가 없음")
                else:
                    if is_first_check:
                        self.logger.warning(f"⚠️ [{code}] chart_cache 없음")
                
                # 매수 로직 로드
                # 차트 데이터가 비어있으면 평가를 건너뜀
                if tic_chart_data.empty:
                    if is_first_check:
                        self.logger.debug(f"ℹ️ [{code}] 틱 차트 데이터가 비어있어 매수 평가를 건너뜁니다.")
                    return signals

                buy_strategies = []

                strategy_name_in_config = strategy_name

                # 개별 전략 섹션에서 매수 조건 가져오기
                if strategy_name_in_config in self.strategy_config:
                    strategy_conf = self.strategy_config[strategy_name_in_config]
                    items = sorted([item for item in strategy_conf.items() if item[0].startswith('buy_stg_')], key=lambda x: int(x[0].split('_')[-1]))
                    items.sort(key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 999)
                    for key, value in items:
                        try:
                            strategy_data = json.loads(value)
                            buy_strategies.append(strategy_data)
                        except json.JSONDecodeError:
                            if is_first_check: # type: ignore
                                self.logger.warning(f"⚠️ [{code}] 매수 로직 파싱 실패: {key}")
                    if buy_strategies and is_first_check:
                        self.logger.debug(f"✅ [{code}] strategy_config에서 매수 로직 {len(buy_strategies)}개 로드됨: {strategy_name_in_config}")

                # 전략이 없으면 매수 평가를 진행하지 않음
                if not buy_strategies:
                    if is_first_check:
                        self.logger.warning(f"⚠️ [{code}] 매수 로직 없음 - 매수 평가를 진행하지 않습니다.")
                    return signals
                
                if is_first_check:
                    self.logger.debug(f"✅ [{code}] 최종 매수 로직 {len(buy_strategies)}개 준비 완료")
                
                # strategy_utils를 사용하여 매수 로직 평가
                safe_locals = strategy_utils.prepare_buy_strategy_locals(
                    code, tic_chart_data, min_chart_data, portfolio, realtime_metrics=realtime_metrics
                )
                condition_met, matched_strategy = strategy_utils.evaluate_strategies(
                    buy_strategies, safe_locals, code, "매수"
                )
                
                if is_first_check:
                    self.logger.debug(f"📊 [{code}] 매수 로직 평가 결과: {condition_met}")

                if condition_met and matched_strategy:
                    self.logger.info(f"📈 매수 신호 발생: {code} - {matched_strategy.get('name', '')}")
                    
                    # [추가] 지표 값 로그 출력
                    self._log_indicator_values(code, matched_strategy.get('content', ''), safe_locals)
                    
                    # 매수 수량 계산: (예수금 / (최대보유종목수 - 현재보유종목수)) / 현재가
                    try:
                        # 1. 투자가능 현금 조회 (캐시 사용)
                        available_cash = await self.trader.get_available_cash()
                        
                        # 2. 남은 매수 가능 종목 수 계산
                        max_holdings = portfolio['max_holdings']
                        current_holdings_count = portfolio['total_holdings']
                        remaining_slots = max_holdings - current_holdings_count
                        
                        if remaining_slots <= 0:
                            self.logger.warning(f"⚠️ [{code}] 매수 불가: 보유 종목 수 한도 초과 ({current_holdings_count}/{max_holdings})")
                            return signals

                        # 3. 1종목당 배정할 금액 계산
                        # 예수금의 98%만 사용하여 미수 발생 방지 및 수수료 여유 확보
                        safe_cash = available_cash * 0.98
                        amount_per_stock = safe_cash / remaining_slots
                        
                        # 4. 수량 계산
                        # 현재가 결정 (틱 데이터 -> 분봉 데이터 순)
                        current_price = 0
                        if not tic_chart_data.empty and 'close' in tic_chart_data.columns:
                            current_price = tic_chart_data['close'].iloc[-1]
                        elif not min_chart_data.empty and 'close' in min_chart_data.columns:
                            current_price = min_chart_data['close'].iloc[-1]
                            
                        if current_price > 0:
                            quantity = int(amount_per_stock / current_price)
                        else:
                            quantity = 1 # 현재가가 0인 경우 (오류 방지)
                            self.logger.warning(f"⚠️ [{code}] 현재가를 찾을 수 없어 수량을 1로 설정합니다. (틱/분봉 데이터 부족)")
                            
                        # 최소 1주 이상 매수
                        if quantity < 1:
                            self.logger.warning(f"⚠️ [{code}] 매수 수량 계산 결과 0주 (예수금 부족): 배정금액 {amount_per_stock:,.0f}원 / 현재가 {current_price:,}원")
                            quantity = 1 # 최소 1주는 매수 시도
                        
                        self.logger.debug(f"💰 매수 수량 계산: {quantity}주 (예수금 {available_cash:,.0f}원 / 남은슬롯 {remaining_slots}개 = 종목당 {amount_per_stock:,.0f}원 / 현재가 {current_price:,}원)")

                    except Exception as qty_ex:
                        self.logger.error(f"매수 수량 계산 중 오류: {qty_ex}", exc_info=True)
                        quantity = 1 # 오류 시 기본값
                    
                    signals.append({
                        'strategy': matched_strategy.get('name', strategy_name),
                        'code': code,
                        'quantity': quantity,
                        'price': 0,  # 시장가
                        'reason': f"기술적 지표 기반 매수 조건 충족: {matched_strategy.get('name', '')}"
                    })
                
                return signals

        except Exception as ex:
            self.logger.error(f"매수 신호 생성 실패 ({code}): {ex}", exc_info=True)
            traceback.print_exc()
            return []
    
    async def get_sell_signals(self, code, market_data, strategy_name):
        """매도 신호 생성 - strategy_utils를 사용한 기술적 지표 기반 평가 (비동기)"""
        try:
            signals = []
            
            # 디버그: 최초 1회만 매도 평가 시작 로그
            if not hasattr(self, '_sell_eval_codes'):
                self._sell_eval_codes = set()
            is_first_sell_check = code not in self._sell_eval_codes
            if is_first_sell_check:
                self._sell_eval_codes.add(code)
            
            # 보유 중인 종목인지 확인
            portfolio = self.trader.get_portfolio_status()
            if code not in portfolio['holdings']:
                # 매도 불가 로그 제거 (너무 빈번함)
                return signals


            
            # 최초 매도 평가 시작 로그
            if is_first_sell_check: # type: ignore
                self.logger.debug(f"🔍 [{code}] 매도 평가 시작 (전략: {strategy_name})")
            
            # 보유 정보
            holding_info = portfolio['holdings'][code]
            
            # 매수 시 사용된 전략 확인
            buy_strategy_name = holding_info.get('buy_strategy', strategy_name)
            # 매수 로직 이름에서 섹션 이름 추출 (예: "[급등주] 2순위..." -> "급등주")
            if buy_strategy_name and buy_strategy_name.startswith('['):
                try:
                    strategy_name = buy_strategy_name.split(']')[0][1:]
                except Exception: pass
            
            # 보유 정보
            holding_info = portfolio['holdings'][code]
            buy_price = portfolio['buy_prices'].get(code, 0)
            buy_time = portfolio['buy_times'].get(code)
            quantity = holding_info.get('quantity', 0)
            
            if buy_price <= 0 or quantity <= 0:
                # 보유 정보 불완전 로그 제거 (너무 빈번함)
                return signals

            # 매수 후 최소 보유 시간(Grace Period) 확인 - 제거 (초단타 대응: 즉시 매도 가능하도록 변경)
            # min_hold_seconds = getattr(self.trader, 'min_hold_seconds', 0)
            # if min_hold_seconds > 0 and buy_time:
            #     time_since_buy = (datetime.now() - buy_time).total_seconds()
            #     if time_since_buy < min_hold_seconds:
            #         if is_first_sell_check: # type: ignore
            #             self.logger.debug(f"⏳ [{code}] 매수 후 {time_since_buy:.1f}초 경과. 매도 평가 유예 중 (최소 {min_hold_seconds}초)")
            #         return signals # 유예 시간 동안 매도 신호 생성 안 함

            
            # 최고가 실시간 업데이트
            current_price = market_data.get('current_price', 0)
            if current_price > 0:
                # 최고가가 없거나 현재가가 더 높으면 업데이트
                if code not in self.trader.highest_prices:
                    self.trader.highest_prices[code] = current_price
                elif current_price > self.trader.highest_prices[code]:
                    old_highest = self.trader.highest_prices[code]
                    self.trader.highest_prices[code] = current_price
                    self.logger.info(f"📈 {code} 최고가 갱신: {old_highest:,}원 → {current_price:,}원")
                
                # 포트폴리오 딕셔너리에 업데이트된 최고가 반영
                portfolio['highest_prices'] = self.trader.highest_prices.copy() # type: ignore
            
            # 하드코딩된 이동 손절 로직을 제거하고 .env의 전략 평가로 통합합니다.

            # 차트 데이터 가져오기 (틱/분봉)
            tic_chart_data = pd.DataFrame()
            min_chart_data = pd.DataFrame()
            realtime_metrics = {}
            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                cache_data = self.parent.chart_cache.get_cached_data(code)
                if cache_data:
                    tic_data = cache_data.get('tic_data', {})
                    min_data = cache_data.get('min_data', {})
                    previous_close = cache_data.get('previous_close', 0)
                    current_open = cache_data.get('current_open', 0)
                    realtime_metrics = cache_data.get('realtime_metrics', {})
                    if tic_data:
                        try:
                            # DataFrame 생성 시 모든 배열 길이가 동일해야 함.
                            min_len = min(len(v) for k, v in tic_data.items() if isinstance(v, list))
                            trimmed_tic_data = {k: v[:min_len] for k, v in tic_data.items() if isinstance(v, list)}
                            tic_chart_data = pd.DataFrame(trimmed_tic_data).dropna().reset_index(drop=True)
                        except Exception:
                            tic_chart_data = pd.DataFrame()
                    if min_data:
                        try:
                            min_chart_data = pd.DataFrame(min_data).dropna().reset_index(drop=True)
                        except Exception:
                            min_chart_data = pd.DataFrame()
            
            # 매도 로직 로드
            sell_strategies = []

            strategy_name_in_config = strategy_name
            
            # strategy_config에서 현재 전략의 매도 조건 가져오기
            if strategy_name_in_config in self.strategy_config:
                strategy_conf = self.strategy_config[strategy_name_in_config]
                # sell_stg_로 시작하는 키들을 찾아서 파싱 (숫자 순서로 정렬)
                sell_stg_items = [(key, value) for key, value in strategy_conf.items() if key.startswith('sell_stg_')]
                # 숫자 순서로 정렬 (sell_stg_1, sell_stg_2, ... 순서)
                sell_stg_items.sort(key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 999)
                executed_rules = self.trader.executed_sell_rules.get(code, set())
                
                for key, value in sell_stg_items:
                    try:
                        strategy_data = json.loads(value)
                        rule_name = strategy_data.get('name', '')
                        # 이미 발동된 이력이 있는 매도 룰은 전략 리스트에서 제외 (중복 방지)
                        if rule_name and rule_name in executed_rules:
                            continue
                        sell_strategies.append(strategy_data)
                    except json.JSONDecodeError:
                        self.logger.debug(f"매도 로직 파싱 실패 ({code}): {key}", exc_info=True)
            
            # 전략이 없으면 매도 평가를 진행하지 않음
            if not sell_strategies:
                if is_first_sell_check: # type: ignore
                    self.logger.warning(f"⚠️ [{code}] 매도 로직 없음 - 매도 평가를 진행하지 않습니다.")
                return signals
            else:
                if is_first_sell_check: # type: ignore
                    self.logger.debug(f"✅ [{code}] 매도 로직 {len(sell_strategies)}개 로드됨: {strategy_name}")
            
            # 현재 수익률 계산 (전략 평가 전에)
            current_price = market_data.get('current_price', 0)
            profit_rate = (current_price - buy_price) / buy_price * 100 if buy_price > 0 else 0
            
            # 손절 조건 도달 시 디버그 로그 (자주 출력되지 않도록 조건부)
            if profit_rate < -0.6:  # 손절 기준 근처일 때만 디버그 # type: ignore
                self.logger.debug(f"🔍 [{code}] 손절 조건 도달 확인: 수익률={profit_rate:.2f}%, 매입가={buy_price:,}원, 현재가={current_price:,}원")
                self.logger.debug(f"🔍 [{code}] 로드된 매도 로직 수: {len(sell_strategies)}개")
                for idx, stg in enumerate(sell_strategies):
                    logging.debug(f"🔍 [{code}] 전략 {idx+1}: {stg.get('name', 'N/A')} - 조건: {stg.get('content', 'N/A')}")

            # strategy_utils를 사용하여 매도 로직 평가
            safe_locals = strategy_utils.prepare_sell_strategy_locals(
                code, tic_chart_data, min_chart_data, buy_price, buy_time, portfolio, 
                current_price=current_price,
                commission_rate=self.trader.commission_rate, 
                tax_rate=self.trader.tax_rate,
                realtime_metrics=realtime_metrics
            )
            condition_met, matched_strategy = strategy_utils.evaluate_strategies(
                sell_strategies, safe_locals, code, "매도"
            )
                
            if condition_met and matched_strategy:
                # 매도 조건 충족 시, 실제 주문 가능한 수량을 REST API로 최종 확인
                # 부분 익절 비율 확인
                partial_sell_ratio = matched_strategy.get('partial_sell_ratio')

                order_available_qty = 0
                try:
                    # 1. 웹소켓 실시간 잔고 데이터 우선 사용 (속도 빠름, API 제한 없음)
                    if (hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'websocket_client')):
                        ws_balance_data = self.parent.login_handler.websocket_client.balance_data
                        if ws_balance_data and code in ws_balance_data:
                            # 주문가능수량 사용
                            order_available_qty = ws_balance_data[code].get('order_available_qty', 0)
                            
                            # 만약 주문가능수량이 0이면 보유수량 사용 (가끔 주문가능수량이 갱신 안 될 때 대비)
                            if order_available_qty <= 0:
                                order_available_qty = ws_balance_data[code].get('quantity', 0)

                            if partial_sell_ratio and 0 < partial_sell_ratio < 1:
                                total_holding_qty = ws_balance_data[code].get('quantity', 0)
                                order_available_qty = int(total_holding_qty * partial_sell_ratio)
                                self.logger.info(f"💰 부분 익절 수량 계산 (웹소켓): 보유수량 {total_holding_qty}주 * {partial_sell_ratio} = {order_available_qty}주")
                            else:
                                self.logger.info(f"⚡ 전량 매도 수량 조회 (웹소켓): {code} 주문가능수량 {order_available_qty}주")

                    # 2. 웹소켓 데이터가 없거나 이상할 경우 REST API 사용 (Fallback)
                    if order_available_qty <= 0:
                        if hasattr(self.parent, 'login_handler') and self.parent.login_handler and hasattr(self.parent.login_handler, 'kiwoom_client'):
                            self.logger.debug(f"⚠️ 웹소켓 잔고 없음, REST API로 재확인 시도: {code}")
                            balance_result = await self.parent.login_handler.kiwoom_client.get_acnt_balance()
                            if balance_result:
                                api_holdings = balance_result.get('stk_acnt_evlt_prst', balance_result.get('output1', []))
                                for stock in api_holdings:
                                    raw_code = stock.get('stk_cd', stock.get('pdno', ''))
                                    stock_code = self.parent.data_manager.normalize_stock_code(raw_code)
                                    if stock_code == code:
                                        total_holding_qty = self.parent.data_manager.safe_int(stock.get('hldg_qty', 0))
                                        if partial_sell_ratio and 0 < partial_sell_ratio < 1:
                                            order_available_qty = int(total_holding_qty * partial_sell_ratio)
                                            self.logger.info(f"📡 부분 익절 수량 계산 (API): 보유수량 {total_holding_qty}주 * {partial_sell_ratio} = {order_available_qty}주")
                                        else:
                                            order_available_qty = self.parent.data_manager.safe_int(stock.get('rmnd_qty', 0))
                                            self.logger.info(f"📡 전량 매도 수량 조회 (API): {code} 주문가능수량 {order_available_qty}주")
                                        break

                except Exception as qty_check_ex:
                    self.logger.error(f"주문가능수량 확인 중 오류 ({code}): {qty_check_ex}", exc_info=True)

                if order_available_qty <= 0:
                    self.logger.warning(f"⚠️ 매도 신호가 발생했으나 주문가능수량이 0주입니다. (다른 주문 처리 중일 수 있음): {code}")
                    return signals

                strategy_display_name = matched_strategy.get('name', strategy_name)
                self.logger.info(f"📉 매도 신호 발생: {code} - {strategy_display_name}")
                self.logger.debug(f"💰 매입가={buy_price:,}원, 현재가={current_price:,}원, 수익률={profit_rate:.2f}%")
                self.logger.debug(f"📊 보유수량={quantity:,}주, 주문가능수량={order_available_qty:,}주, 매도수량={order_available_qty:,}주")
                
                # [추가] 지표 값 로그 출력
                self._log_indicator_values(code, matched_strategy.get('content', ''), safe_locals)
                
                signals.append({
                    'strategy': matched_strategy.get('name', strategy_name),
                    'code': code,
                    'quantity': order_available_qty,  # 주문가능수량만큼만 매도
                    'price': 0,  # 시장가
                    'reason': f"기술적 지표 기반 매도 조건 충족: {matched_strategy.get('name', '')} (수익률: {profit_rate:.2f}%)"
                })
            else:
                # 매도 조건 미충족 시 현재 수익률 표시 (최초 1회만)
                if is_first_sell_check: # type: ignore
                    self.logger.debug(f"ℹ️ [{code}] 매도 조건 미충족 (보유 중, 수익률: {profit_rate:.2f}%)")
            
            return signals
            
        except Exception as ex:
            self.logger.error(f"매도 신호 생성 실패 ({code}): {ex}", exc_info=True)
            traceback.print_exc()
            return []
    
    async def execute_buy_signals(self, code, signals):
        """매수 신호 실행 (비동기)"""
        try:
            for signal in signals:
                success = await self.trader.place_buy_order(
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
            self.logger.error(f"매수 신호 실행 실패 ({code}): {ex}", exc_info=True)
    
    async def execute_sell_signals(self, code, signals):
        """매도 신호 실행 (비동기)"""
        try:
            for idx, signal in enumerate(signals):
                requested_quantity = signal['quantity']
                
                # 실제 주문 전 주문가능수량 최종 확인 (웹소켓 실시간 데이터)
                actual_order_available_qty = requested_quantity  # 기본값
                try:
                    if (hasattr(self.parent, 'login_handler') and self.parent.login_handler and
                        hasattr(self.parent.login_handler, 'websocket_client') and self.parent.login_handler.websocket_client and
                        hasattr(self.parent.login_handler.websocket_client, 'balance_data')):
                        
                        ws_balance_data = self.parent.login_handler.websocket_client.balance_data
                        if ws_balance_data and code in ws_balance_data:
                            actual_order_available_qty = ws_balance_data[code].get('order_available_qty', requested_quantity)
                            if actual_order_available_qty < requested_quantity:
                                self.logger.warning(f"⚠️ [{code}] 주문가능수량 변경 감지: 요청={requested_quantity}주, 실제={actual_order_available_qty}주 (다른 주문으로 인한 변경)")
                except Exception as ws_check_ex:
                    self.logger.debug(f"주문 전 주문가능수량 체크 중 오류 ({code}): {ws_check_ex}", exc_info=True)
                
                # 주문가능수량이 0주 이하면 주문 스킵 및 일시적 전략 평가 중단
                if actual_order_available_qty <= 0:
                    self.logger.warning(f"⚠️ [{code}] 주문가능수량 0주 - 주문 스킵 (이미 매도 주문 진행 중으로 추정)")
                    
                    # 무한 루프 방지를 위해 일시적으로 전략 평가 대상에서 제외 (10초간)
                    if not hasattr(self, '_temp_blocked_codes'):
                        self._temp_blocked_codes = {}
                    
                    # 현재 시간 + 10초
                    self._temp_blocked_codes[code] = datetime.now().timestamp() + 10
                    self.logger.debug(f"⏳ [{code}] 10초간 전략 평가 일시 중단 (주문가능수량 0)")
                    continue
                
                # 실제 주문 가능한 수량으로 제한
                final_quantity = min(requested_quantity, actual_order_available_qty)
                if final_quantity < requested_quantity:
                    self.logger.info(f"📊 [{code}] 주문 수량 조정: {requested_quantity}주 → {final_quantity}주")
                
                success = await self.trader.place_sell_order(
                    code, 
                    final_quantity,  # 조정된 수량 사용
                    signal['price'], 
                    signal['strategy']
                )
                
                if success:
                    self.logger.debug(f"✅ [{code}] 매도 주문 성공: {signal['strategy']} - {final_quantity}주")
                    
                    # 재매수 대기 시간(Cooldown)을 위한 매도 시간 기록 (전량/분할매도 무관하게 타이머 리셋)
                    if not hasattr(self.trader, 'last_sell_times'):
                        self.trader.last_sell_times = {}
                    self.trader.last_sell_times[code] = datetime.now()
                    
                    # 방어 로직: 실행 성공한 매도 룰의 이름을 기록하여 해당 보유 턴에서 중복 발동 방지
                    if code not in self.trader.executed_sell_rules:
                        self.trader.executed_sell_rules[code] = set()
                    self.trader.executed_sell_rules[code].add(signal['strategy'])
                    
                    self.signal_strategy_result.emit(
                        code, 
                        "sell", 
                        {
                            'strategy': signal['strategy'],
                            'reason': signal['reason'],
                            'quantity': final_quantity,  # 실제 주문된 수량
                            'price': signal['price']
                        }
                    )

                    # 추적손절(Trailing Stop)로 매도된 경우 당일 재매수 금지(Blacklist) 추가 로직 제거됨
                    # (사용자 요청: 추적손절 매도시 당일 매수 금지 목록에 추가하는 기능을 없애 줘)
                else:
                    self.logger.error(f"❌ [{code}] 매도 주문 실패: {signal['strategy']} - {final_quantity}주")
                    
        except Exception as ex:
            self.logger.error(f"매도 신호 실행 실패 ({code}): {ex}", exc_info=True)
