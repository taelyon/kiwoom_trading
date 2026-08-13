import logging
import asyncio
from typing import Optional, List, Dict, Any
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
        self.condition_name = self.config.get('SETTINGS', 'swing_condition_name', fallback='스윙_저가매수')
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
        
        self.reload_config()

    def reload_config(self):
        """환경변수 및 설정 파일로부터 스윙 파라미터 및 매수/매도 로직 재로드"""
        try:
            from config_manager import EnvConfigParser
            self.config = EnvConfigParser()
            self.config.reload()
        except Exception:
            pass

        self.is_enabled = self.config.getboolean('SETTINGS', 'swing_enabled', fallback=True)
        self.condition_name = self.config.get('SETTINGS', 'swing_condition_name', fallback='스윙_저가매수')
        self.invest_amount = self.config.getfloat('SETTINGS', 'swing_invest_amount', fallback=2000000.0)
        self.max_holdings = self.config.getint('SETTINGS', 'swing_max_holdings', fallback=3)
        self.target_profit_pct = self.config.getfloat('SETTINGS', 'swing_target_profit', fallback=5.0)
        self.stop_loss_pct = self.config.getfloat('SETTINGS', 'swing_stop_loss', fallback=-10.0)
        self.first_entry_ratio = self.config.getfloat('SETTINGS', 'swing_first_entry_ratio', fallback=0.4) # 1차 진입비중 40% (30~50%)

        default_buy_str = json.dumps([
            {
                "name": "스윙_시간통제_1520_1530",
                "type": "TIME",
                "content": "152000 <= time_int <= 153000"
            },
            {
                "name": "스윙_저가매수_눌림목",
                "type": "TECHNICAL",
                "content": "98.0 <= disparity20 <= 105.0 and rsi14 < 70.0 and volume_ratio >= 1.2 and price_roc1 > -2.0"
            }
        ], ensure_ascii=False)

        default_sell_str = json.dumps([
            {
                "name": "스윙_1차익절_50%매도",
                "type": "PARTIAL_PROFIT",
                "content": "current_profit_pct >= 5.0 and not partially_sold"
            },
            {
                "name": "스윙_본전방어_전량매도",
                "type": "BREAKEVEN_STOP",
                "content": "partially_sold and current_profit_pct <= 0.5"
            },
            {
                "name": "스윙_추세종료_과매수이탈_전량매도",
                "type": "TREND_EXIT",
                "content": "rsi14 < 70.0 and prev_rsi14 >= 70.0"
            },
            {
                "name": "스윙_추세종료_마지노선붕괴_전량매도",
                "type": "MA_EXIT",
                "content": "(ma5 < ma10 and prev_ma5 >= prev_ma10) or (current_price < ma20 and prev_price >= prev_ma20)"
            },
            {
                "name": "스윙_세력방어선붕괴_기준봉손절",
                "type": "STOP_LOSS",
                "content": "current_price < base_candle_low or current_profit_pct <= -10.0"
            }
        ], ensure_ascii=False)

        raw_buy = self.config.get('SETTINGS', 'swing_buy_strategy', fallback=default_buy_str)
        raw_sell = self.config.get('SETTINGS', 'swing_sell_strategy', fallback=default_sell_str)

        try:
            self.buy_strategies = json.loads(raw_buy) if isinstance(raw_buy, str) and raw_buy.strip() else json.loads(default_buy_str)
        except Exception:
            self.buy_strategies = json.loads(default_buy_str)

        try:
            self.sell_strategies = json.loads(raw_sell) if isinstance(raw_sell, str) and raw_sell.strip() else json.loads(default_sell_str)
        except Exception:
            self.sell_strategies = json.loads(default_sell_str)

        self.logger.info(f"🔄 SwingManager 설정 리로드 완료 (매수룰: {len(self.buy_strategies)}개, 매도룰: {len(self.sell_strategies)}개)")

    async def initialize(self):
        """스윙 매니저 초기화 및 DB에서 보유 종목 로드"""
        try:
            await self.db_manager.init_database()
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

            # 조건검색 목록에서 지정된 조건식 seq 찾기
            cond_list = getattr(self.parent, 'condition_search_list', []) or []
            target_index = None
            for cond in cond_list:
                title = str(cond.get('title', '')).strip()
                target_cond = str(self.condition_name).strip()
                if title == target_cond or target_cond in title or title in target_cond:
                    target_index = cond.get('seq') if cond.get('seq') is not None else cond.get('index')
                    self.logger.info(f"✅ 스윙 조건검색식 매칭 성공: '{title}' (Seq: {target_index})")
                    break

            if target_index is not None:
                # 조건검색 요청 전송 (search_type: 0 = 일반조회, 실시간 등록 안 함)
                await ws_client.send_message({
                    'trnm': 'CNSRREQ',
                    'seq': str(target_index),
                    'search_type': '0',
                    'stex_tp': 'K',
                    'cont_yn': 'N',
                    'next_key': ''
                })
                self.logger.info(f"✅ [스윙 수동/자동] '{self.condition_name}' (Seq: {target_index}) 검색 요청 완료")
            else:
                self.logger.warning(f"⚠️ [스윙 수동/자동] 조건검색식 '{self.condition_name}'을 찾을 수 없습니다. (현재 수신된 키움 조건식: {len(cond_list)}개)")

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

                safe_locals['time_int'] = int(datetime.now().strftime("%H%M%S"))
                safe_locals['target_profit'] = self.target_profit_pct
                safe_locals['stop_loss'] = self.stop_loss_pct

                # 스윙 동적 매수 로직 평가
                all_passed = True
                for stg in self.buy_strategies:
                    rule_content = stg.get('content', '')
                    if rule_content and not evaluate_swing_condition(rule_content, safe_locals):
                        all_passed = False
                        break

                if all_passed:
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
        """키움 REST API ka10081 (주식일봉차트조회) 호출"""
        try:
            # KiwoomRestClient 참조 확보 (trader.client 또는 login_handler.kiwoom_client)
            client = None
            if hasattr(self.parent, 'trader') and self.parent.trader:
                client = getattr(self.parent.trader, 'client', None)
            if client is None and hasattr(self.parent, 'login_handler') and self.parent.login_handler:
                client = getattr(self.parent.login_handler, 'kiwoom_client', None)
            if client is None:
                self.logger.warning(f"⚠️ [스윙] 일봉 조회 불가: REST 클라이언트 없음 ({code})")
                return None

            today_str = datetime.now().strftime('%Y%m%d')
            resp = await client.get_stock_daily_chart(code, base_dt=today_str, cont_yn='N', next_key='')
            if not resp or resp.get('return_code', -1) != 0:
                self.logger.warning(f"⚠️ [스윙] 일봉 응답 에러 ({code}): {resp.get('return_msg', 'N/A')}")
                return None

            # ka10081 응답 필드: stk_dt_pole_chart_qry 리스트
            items = resp.get('stk_dt_pole_chart_qry', [])
            if not items:
                self.logger.warning(f"⚠️ [스윙] 일봉 데이터 없음 ({code})")
                return None

            rows = []
            for item in items[:200]:  # 최근 200일 (이동평균선이 차트 시작부터 끊김없이 그려지도록 충분한 데이터 확보)
                # ka10081 공식 응답 필드명: dt(일자), open_pric(시가), high_pric(고가), low_pric(저가), cur_prc(현재가/종가), trde_qty(거래량)
                try:
                    dt = item.get('dt', '')
                    open_p = abs(float(item.get('open_pric') or 0))
                    high_p = abs(float(item.get('high_pric') or 0))
                    low_p = abs(float(item.get('low_pric') or 0))
                    close_p = abs(float(item.get('cur_prc') or 0))
                    vol = int(float(item.get('trde_qty') or 0))
                    if dt and close_p > 0:
                        rows.append({
                            'code': code,
                            'datetime': dt,
                            'open': open_p,
                            'high': high_p,
                            'low': low_p,
                            'close': close_p,
                            'volume': vol
                        })
                except (ValueError, TypeError):
                    continue

            if not rows:
                self.logger.warning(f"⚠️ [스윙] 파싱 가능한 일봉 데이터 없음 ({code})")
                return None

            # 💾 DB (daily_candles) 전용 테이블에 자동 영구 저장
            if hasattr(self, 'db_manager') and self.db_manager:
                try:
                    await self.db_manager.save_daily_candles(rows)
                except Exception as db_err:
                    self.logger.error(f"❌ [스윙 DB] daily_candles 저장 실패 ({code}): {db_err}")

            df = pd.DataFrame(rows)
            df.sort_values(by='datetime', ascending=True, inplace=True)
            df.reset_index(drop=True, inplace=True)
            self.logger.debug(f"✅ [스윙] 일봉 데이터 조회 및 DB 저장 완료: {code} ({len(df)}일)")
            return df
        except Exception as e:
            self.logger.error(f"❌ 일봉 데이터 조회 에러 ({code}): {e}")
            return None

    async def _fetch_weekly_candles(self, code: str) -> Optional[pd.DataFrame]:
        """REST API (ka10082)를 통해 주봉 차트 데이터 수집"""
        try:
            client = None
            if hasattr(self.parent, 'trader') and self.parent.trader:
                client = getattr(self.parent.trader, 'client', None)
            if client is None and hasattr(self.parent, 'login_handler') and self.parent.login_handler:
                client = getattr(self.parent.login_handler, 'kiwoom_client', None)
            if client is None:
                self.logger.warning(f"⚠️ [스윙] 주봉 조회 불가: REST 클라이언트 없음 ({code})")
                return None

            today_str = datetime.now().strftime('%Y%m%d')
            resp = await client.get_stock_weekly_chart(code, base_dt=today_str, cont_yn='N', next_key='')
            if not resp or resp.get('return_code', -1) != 0:
                self.logger.warning(f"⚠️ [스윙] 주봉 응답 에러 ({code}): {resp.get('return_msg', 'N/A')}")
                return None

            # ka10082 공식 응답 키: stk_stk_pole_chart_qry
            items = resp.get('stk_stk_pole_chart_qry', resp.get('stk_wkl_pole_chart_qry', resp.get('stk_dt_pole_chart_qry', [])))
            if not items:
                self.logger.warning(f"⚠️ [스윙] 주봉 데이터 없음 ({code})")
                return None

            rows = []
            for item in items[:150]:  # 최근 150주
                try:
                    dt = item.get('dt', '')
                    open_p = abs(float(item.get('open_pric') or 0))
                    high_p = abs(float(item.get('high_pric') or 0))
                    low_p = abs(float(item.get('low_pric') or 0))
                    close_p = abs(float(item.get('cur_prc') or 0))
                    vol = int(float(item.get('trde_qty') or 0))
                    if dt and close_p > 0:
                        rows.append({
                            'datetime': dt,
                            'open': open_p,
                            'high': high_p,
                            'low': low_p,
                            'close': close_p,
                            'volume': vol
                        })
                except (ValueError, TypeError):
                    continue

            if not rows:
                return None

            df = pd.DataFrame(rows)
            df.sort_values(by='datetime', ascending=True, inplace=True)
            df.reset_index(drop=True, inplace=True)
            self.logger.debug(f"✅ [스윙] 주봉 데이터 조회 완료: {code} ({len(df)}주)")
            return df
        except Exception as e:
            self.logger.error(f"❌ 주봉 데이터 조회 에러 ({code}): {e}")
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

            # 2차 추가 매수(물타기) 자동 금지 - 이미 보유 중인 경우 매수 스킵
            if code in self.swing_holdings:
                self.logger.info(f"🛡️ [{code}] 종목은 이미 스윙 보유 중이므로 기계적 2차 추가 매수(물타기)를 자동 스킵합니다.")
                continue

            # 1차 분할 비중 수량 계산 (투자금의 40%만 1차 매수)
            first_entry_amount = self.invest_amount * getattr(self, 'first_entry_ratio', 0.4)
            qty = max(1, int(first_entry_amount / price))
            self.logger.info(f"📈 [스윙 15:28 시장가 1차 매수] [{code}] 1차진입비중(40%): {first_entry_amount:,.0f}원 -> {qty}주 체결 시도 (예상가: {price:,.0f}원)")

            try:
                trader = self.parent.trader
                res = await trader.send_market_buy_order_async(code, qty, strategy="스윙_1차종가매수")
                
                today_str = datetime.now().strftime("%Y-%m-%d")
                stock_name = getattr(self.parent, 'master_code_dict', {}).get(code, code)
                
                await self.db_manager.save_swing_holding(
                    code=code,
                    name=stock_name,
                    buy_price=price,
                    qty=qty,
                    buy_date=today_str,
                    highest_price=price,
                    strategy="스윙_1차종가매수"
                )

                self.swing_holdings[code] = {
                    'code': code,
                    'name': stock_name,
                    'buy_price': price,
                    'qty': qty,
                    'buy_date': today_str,
                    'highest_price': price,
                    'strategy': "스윙_1차종가매수",
                    'partially_sold': False
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
                partially_sold = holding.get('partially_sold', False)

                profit_pct = ((curr_price - buy_price) / buy_price * 100.0) - 0.3 # 세금/수수료 감안

                # 최고가 갱신 DB 업데이트
                if curr_price > holding.get('highest_price', 0):
                    await self.db_manager.update_swing_holding_highest(code, curr_price)

                # 1. 분할 익절 로직: 평단가 대비 +5.0% 달성 시 보유 물량의 50% 분할 매도
                if profit_pct >= self.target_profit_pct and not partially_sold:
                    total_qty = holding['qty']
                    sell_qty = max(1, total_qty // 2)
                    rem_qty = total_qty - sell_qty
                    
                    self.logger.info(f"🟢 [스윙 1차 50% 분할 익절 발동] [{code}] {profit_pct:+.2f}% 달성 -> {sell_qty}주/총{total_qty}주 시장가 매도 전송")
                    res = await self.parent.trader.send_market_sell_order_async(code, sell_qty, strategy="스윙_1차50%익절")
                    
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    p_loss = (curr_price - buy_price) * sell_qty
                    
                    await self.db_manager.save_swing_trade_record(
                        code=code, name=holding['name'], buy_price=buy_price, sell_price=curr_price,
                        qty=sell_qty, profit_loss=p_loss, profit_pct=profit_pct, buy_date=holding['buy_date'],
                        sell_date=today_str, strategy="스윙_1차50%익절"
                    )

                    if rem_qty > 0:
                        holding['qty'] = rem_qty
                        holding['partially_sold'] = True
                        await self.db_manager.update_swing_holding_qty_and_partial(code, rem_qty, True)
                    else:
                        await self.db_manager.delete_swing_holding(code)
                        del self.swing_holdings[code]
                        if code in self.swing_stock_codes: self.swing_stock_codes.remove(code)
                    continue

                is_sell_triggered = False
                sell_reason = ""

                safe_locals = {
                    'buy_price': buy_price,
                    'current_price': curr_price,
                    'current_profit_pct': profit_pct,
                    'highest_price': highest_price,
                    'holding_days': holding.get('holding_days', 1),
                    'target_profit': self.target_profit_pct,
                    'stop_loss': self.stop_loss_pct,
                    'partially_sold': partially_sold
                }

                df_daily = await self._fetch_daily_candles(code)
                if df_daily is not None and not df_daily.empty:
                    safe_locals = prepare_swing_locals(code, df_daily, current_price=curr_price, holding_info=holding)
                else:
                    safe_locals = {
                        'buy_price': buy_price,
                        'current_price': curr_price,
                        'current_profit_pct': profit_pct,
                        'highest_price': highest_price,
                        'holding_days': holding.get('holding_days', 1),
                        'target_profit': self.target_profit_pct,
                        'stop_loss': self.stop_loss_pct,
                        'partially_sold': partially_sold,
                        'rsi14': 50.0, 'prev_rsi14': 50.0,
                        'ma5': curr_price, 'ma10': curr_price, 'ma20': curr_price,
                        'prev_ma5': curr_price, 'prev_ma10': curr_price, 'prev_ma20': curr_price,
                        'base_candle_low': buy_price * 0.90
                    }

                # 스윙 동적 매도 로직 평가 (1.본전방어, 2.RSI70이탈/이평선붕괴, 3.기준봉손절 등)
                for stg in self.sell_strategies:
                    rule_content = stg.get('content', '')
                    if rule_content and evaluate_swing_condition(rule_content, safe_locals):
                        is_sell_triggered = True
                        sell_reason = stg.get('name', '스윙 전략 매도')
                        break

                if not is_sell_triggered and profit_pct <= self.stop_loss_pct:
                    is_sell_triggered = True
                    sell_reason = f"스윙 전량 손절 (-10% 하방 차단: {profit_pct:.2f}%)"

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
