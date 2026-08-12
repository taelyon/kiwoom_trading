import logging
import asyncio
from datetime import datetime, time
import json
import pandas as pd
from config_manager import EnvConfigParser
from database import AsyncDatabaseManager
from swing_strategy_utils import prepare_swing_locals, evaluate_swing_condition, calc_daily_indicators

class SwingManager:
    """
    조건검색식 + 일봉 기술적 분석 기반 장마감(15:20~15:30) 종가 스윙매매 전용 매니저
    """

    def __init__(self, parent_app):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent_app
        self.config = EnvConfigParser()
        self.db_manager = AsyncDatabaseManager()

        # 스윙 설정값 파싱
        self.is_enabled = self.config.getboolean('SETTINGS', 'swing_enabled', fallback=True)
        self.condition_name = self.config.get('SETTINGS', 'swing_condition_name', fallback='스윙_종가돌파')
        self.invest_amount = self.config.getfloat('SETTINGS', 'swing_invest_amount', fallback=2000000.0) # 종목당 200만원
        self.max_holdings = self.config.getint('SETTINGS', 'swing_max_holdings', fallback=3)
        self.target_profit_pct = self.config.getfloat('SETTINGS', 'swing_target_profit', fallback=5.0) # +5% 목표
        self.stop_loss_pct = self.config.getfloat('SETTINGS', 'swing_stop_loss', fallback=-3.0) # -3% 손절

        # 메모리 보관 상태
        self.swing_holdings = {}       # {code: holding_dict}
        self.swing_stock_codes = set() # 초단타 모듈 침범 방지용 4중 격리 세트
        self.candidate_stocks = []     # 15:15~15:25 검색된 후보 종목
        self.executed_today = False    # 당일 15:28 매수 수행 여부

        # 백그라운드 태스크
        self.scheduler_task = None

    async def initialize(self):
        """스윙 매니저 초기화 및 DB에서 보유 종목 로드"""
        try:
            stored_holdings = await self.db_manager.get_swing_holdings()
            self.swing_holdings = stored_holdings
            self.swing_stock_codes = set(stored_holdings.keys())
            self.logger.info(f"🚀 SwingManager 초기화 완료 (보유 종목: {len(self.swing_holdings)}개 - {list(self.swing_stock_codes)})")
        except Exception as e:
            self.logger.error(f"❌ SwingManager 초기화 중 DB 로드 오류: {e}")

    def is_swing_stock(self, code: str) -> bool:
        """해당 종목이 스윙 보유/목표 종목인지 확인 (초단타 침범 방지 1, 2, 3차)"""
        return code in self.swing_stock_codes

    def start_scheduler(self):
        """장마감 타임 스케줄러 기동"""
        if self.scheduler_task is None or self.scheduler_task.done():
            self.scheduler_task = asyncio.create_task(self._run_daily_scheduler())
            self.logger.info("⏱️ SwingManager 스케줄러 태스크 시작됨")

    async def _run_daily_scheduler(self):
        """매일 15:15 ~ 15:30 타임라인 및 실시간 스윙 감시 루프"""
        await self.initialize()
        
        while True:
            try:
                now = datetime.now()
                # 주말(토, 일)에는 대기
                if now.weekday() >= 5:
                    await asyncio.sleep(60)
                    continue

                curr_time = now.time()

                # 날짜가 바뀌면 당일 실행 플래그 리셋
                if curr_time < time(9, 0):
                    self.executed_today = False

                if self.is_enabled:
                    # 1. 15:15 ~ 15:20: 스윙 전용 조건검색식 후보 수신
                    if time(15, 15) <= curr_time < time(15, 20) and not self.executed_today:
                        await self._fetch_swing_candidates()

                    # 2. 15:20 ~ 15:27: 후보 종목 일봉 기술분석 및 스윙 매수 평가
                    elif time(15, 20) <= curr_time < time(15, 28) and not self.executed_today:
                        await self._evaluate_and_select_buy_targets()

                    # 3. 15:28:00: 확정 종목 옵션 A (15:28 시장가) 주문 송신
                    elif time(15, 28) <= curr_time < time(15, 29) and not self.executed_today:
                        await self._execute_swing_buy_orders()

                    # 4. 장중 및 장마감 시점 보유 스윙 종목 매도 룰 감시
                    if time(9, 0) <= curr_time <= time(15, 30):
                        await self._check_swing_exit_rules()

                await asyncio.sleep(10) # 10초마다 루프 검사
            except Exception as e:
                self.logger.error(f"❌ SwingManager 스케줄러 루프 오류: {e}")
                await asyncio.sleep(10)

    async def _fetch_swing_candidates(self):
        """15:15 스윙 전용 키움 조건검색식 편입 종목 목록 수신"""
        try:
            self.logger.info(f"🔍 [스윙 15:15] '{self.condition_name}' 조건검색식 수신 시도...")
            
            ws_client = getattr(self.parent.login_handler, 'websocket_client', None)
            if not ws_client:
                self.logger.warning("⚠️ 웹소켓 미연결로 조건검색 수신 불가")
                return

            # 조건검색 목록에서 지정된 조건식 index 찾기
            cond_list = getattr(self.parent, 'condition_search_list', [])
            target_index = None
            for cond in cond_list:
                if cond.get('title') == self.condition_name:
                    target_index = cond.get('index')
                    break

            if target_index is not None:
                # 조건검색 요청 전송
                await ws_client.send_message({
                    'trnm': 'CNSRREQ',
                    'seq': target_index,
                    'search_type': '0' # 일반조회
                })
                self.logger.info(f"✅ [스윙 15:15] '{self.condition_name}' (Seq: {target_index}) 검색 요청 완료")
            else:
                self.logger.warning(f"⚠️ [스윙 15:15] 조건검색식 '{self.condition_name}'을 찾을 수 없습니다. (전체 보유종목 기반 평가 진행)")

        except Exception as e:
            self.logger.error(f"❌ 스윙 조건검색 수신 오류: {e}")

    async def set_candidate_codes(self, codes: list):
        """웹소켓 수신 조건검색 종목 리스트 갱신"""
        self.candidate_stocks = codes
        # 초단타 침범 방지를 위해 스윙 후보 세트에도 등록
        for c in codes:
            self.swing_stock_codes.add(c)
        self.logger.info(f"📋 [스윙 후보 등록] 총 {len(codes)}개 종목: {codes[:5]}...")

    async def _evaluate_and_select_buy_targets(self):
        """15:20~15:27 후보 종목 일봉 기술분석 및 매수 최종 대상 선정"""
        if not self.candidate_stocks:
            return

        # 현재 스윙 슬롯 확인
        current_hold_cnt = len(self.swing_holdings)
        available_slots = self.max_holdings - current_hold_cnt
        if available_slots <= 0:
            self.logger.info(f"ℹ️ 스윙 최대 보유 수량({self.max_holdings}개) 도달로 신규 매수 스킵")
            return

        selected_targets = []
        for code in self.candidate_stocks:
            # 이미 스윙 보유 중인 종목 제외
            if code in self.swing_holdings:
                continue

            try:
                # 키움 REST API로 일봉 데이터 60일분 조회
                df_daily = await self._fetch_daily_candles(code)
                if df_daily is None or df_daily.empty:
                    continue

                safe_locals = prepare_swing_locals(code, df_daily)
                if not safe_locals:
                    continue

                # 스윙 매수 기술적 분석 조건 검증
                # 룰 1: 20일 이격도 98 이상 105 이하 (눌림목/돌파)
                # 룰 2: RSI 14 < 70 (과매수 제외)
                # 룰 3: 거래량 비율 >= 1.2 (평소 대비 거래량 분출)
                rule = "98.0 <= disparity20 <= 105.0 and rsi14 < 70.0 and volume_ratio >= 1.2 and price_roc1 > -2.0"
                if evaluate_swing_condition(rule, safe_locals):
                    selected_targets.append({
                        'code': code,
                        'score': safe_locals.get('volume_ratio', 1.0) * safe_locals.get('disparity20', 100.0),
                        'price': safe_locals.get('current_price', 0.0)
                    })
                    self.logger.info(f"🎯 [스윙 매수 후보 통과] [{code}] 이격도: {safe_locals.get('disparity20'):.1f}, RSI: {safe_locals.get('rsi14'):.1f}, 거래량비율: {safe_locals.get('volume_ratio'):.2f}")

            except Exception as e:
                self.logger.error(f"❌ 스윙 지표 평가 에러 ({code}): {e}")

        # 점수 높은 순 정렬 후 슬롯만큼 최종 선정
        selected_targets.sort(key=lambda x: x['score'], reverse=True)
        self.final_buy_targets = selected_targets[:available_slots]

    async def _fetch_daily_candles(self, code: str) -> pd.DataFrame:
        """키움 REST API opt10081 (주식일봉차트조회) 호출"""
        try:
            client = getattr(self.parent.trader, 'client', None)
            if not client:
                return None

            resp = await client.request_opt10081_async(code, date="", orig_type="1")
            if not resp or 'output' not in resp:
                return None

            items = resp['output']
            if not items:
                return None

            rows = []
            for item in items[:60]: # 최근 60일
                rows.append({
                    'datetime': item.get('일자'),
                    'open': float(item.get('시가', 0)),
                    'high': float(item.get('고가', 0)),
                    'low': float(item.get('저가', 0)),
                    'close': float(item.get('현재가', 0)),
                    'volume': int(item.get('거래량', 0))
                })
            df = pd.DataFrame(rows)
            df.sort_values(by='datetime', ascending=True, inplace=True)
            return df
        except Exception as e:
            self.logger.error(f"❌ 일봉 데이터 조회 에러 ({code}): {e}")
            return None

    async def _execute_swing_buy_orders(self):
        """15:28:00 확정 종목 옵션 A (15:28 시장가 주문 제출) 실행"""
        self.executed_today = True
        targets = getattr(self, 'final_buy_targets', [])
        if not targets:
            self.logger.info("ℹ️ [스윙 15:28] 당일 최종 매수 대상 종목 없음")
            return

        for target in targets:
            code = target['code']
            price = target['price']
            if price <= 0:
                continue

            # 주문 수량 계산
            qty = max(1, int(self.invest_amount / price))
            self.logger.info(f"📈 [스윙 15:28 시장가 매수 송신] [{code}] 수량: {qty}주 (예상가: {price:,.0f}원)")

            try:
                # 키움 REST API 시장가 주문 제출 (옵션 A)
                trader = self.parent.trader
                res = await trader.send_market_buy_order_async(code, qty, strategy="스윙_종가매수")
                
                # 스윙 보유 DB 기록
                today_str = datetime.now().strftime("%Y-%m-%d")
                stock_name = getattr(self.parent, 'master_code_dict', {}).get(code, code)
                
                await self.db_manager.save_swing_holding(
                    code=code,
                    name=stock_name,
                    buy_price=price,
                    qty=qty,
                    buy_date=today_str,
                    highest_price=price,
                    strategy="스윙_종가매수"
                )

                # 메모리 저장
                self.swing_holdings[code] = {
                    'code': code,
                    'name': stock_name,
                    'buy_price': price,
                    'qty': qty,
                    'buy_date': today_str,
                    'highest_price': price,
                    'strategy': "스윙_종가매수"
                }
                self.swing_stock_codes.add(code)

            except Exception as e:
                self.logger.error(f"❌ 스윙 매수 주문 송신 실패 ({code}): {e}")

    async def _check_swing_exit_rules(self):
        """스윙 보유 종목의 목표가/손절가 매도 감시"""
        if not self.swing_holdings:
            return

        for code, holding in list(self.swing_holdings.items()):
            try:
                # 현재가 조회 (체결잔고 캐시 또는 실시간 시세)
                curr_price = float(self.parent.trader.balance_data.get(code, {}).get('current_price', 0.0))
                if curr_price <= 0:
                    curr_price = holding['buy_price']

                buy_price = holding['buy_price']
                highest_price = max(holding.get('highest_price', buy_price), curr_price)
                holding['highest_price'] = highest_price

                # 수익률 계산
                profit_pct = ((curr_price - buy_price) / buy_price * 100.0) - 0.3 # 세금/수수료 감안

                # 최고가 갱신 DB 업데이트
                if curr_price > holding.get('highest_price', 0):
                    await self.db_manager.update_swing_holding_highest(code, curr_price)

                is_sell_triggered = False
                sell_reason = ""

                # 1. 목표가 달성 (예: +5%)
                if profit_pct >= self.target_profit_pct:
                    is_sell_triggered = True
                    sell_reason = f"스윙 목표가 달성 (+{profit_pct:.2f}%)"

                # 2. 손절가 이탈 (예: -3%)
                elif profit_pct <= self.stop_loss_pct:
                    is_sell_triggered = True
                    sell_reason = f"스윙 손절가 이탈 ({profit_pct:.2f}%)"

                if is_sell_triggered:
                    self.logger.info(f"📉 [스윙 매도 발동] [{code}] {sell_reason} -> 전량 시장가 매도")
                    
                    qty = holding['qty']
                    res = await self.parent.trader.send_market_sell_order_async(code, qty, strategy=sell_reason)

                    profit_loss = (curr_price - buy_price) * qty
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    
                    # DB 이력 저장 및 보유 삭제
                    await self.db_manager.save_swing_trade_record(
                        code=code,
                        name=holding['name'],
                        buy_price=buy_price,
                        sell_price=curr_price,
                        qty=qty,
                        profit_loss=profit_loss,
                        profit_pct=profit_pct,
                        buy_date=holding['buy_date'],
                        sell_date=today_str,
                        strategy=sell_reason
                    )
                    await self.db_manager.delete_swing_holding(code)

                    # 메모리 삭제
                    del self.swing_holdings[code]
                    if code in self.swing_stock_codes:
                        self.swing_stock_codes.remove(code)

            except Exception as e:
                self.logger.error(f"❌ 스윙 매도 감시 오류 ({code}): {e}")
