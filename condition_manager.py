import logging
import asyncio

from config_manager import EnvConfigParser


class ConditionSearchManager:
    """조건검색 관리 매니저"""
    
    def __init__(self, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
        self.is_manual_change = False # 사용자의 수동 변경 여부 플래그

    async def handle_condition_search_list_query(self):
        """조건검색 목록조회 (웹소켓 기반)"""
        try:
            self.logger.debug("🔍 조건검색 목록조회 시작 (웹소켓)")

            if not hasattr(self.parent, 'trader') or not self.parent.trader:
                self.logger.warning("⚠️ 트레이더가 초기화되지 않았습니다")
                return

            if not hasattr(self.parent.trader, 'client') or not self.parent.trader.client:
                self.logger.warning("⚠️ API 클라이언트가 연결되지 않았습니다")
                return

            # 웹소켓 클라이언트 확인
            if not hasattr(self.parent.login_handler, 'websocket_client') or not self.parent.login_handler.websocket_client:
                self.logger.warning("⚠️ 웹소켓 클라이언트가 연결되지 않았습니다")
                return

            # 웹소켓을 통한 조건검색 목록조회
            try:
                await self.parent.login_handler.websocket_client.send_message({
                    'trnm': 'CNSRLST', # TR명
                })
                logging.debug("✅ 조건검색 목록조회 요청 전송 완료 (웹소켓)") # type: ignore

                # 웹소켓 응답은 receive_messages에서 처리됨
                logging.debug("💾 조건검색 목록조회 요청 완료 - 응답은 웹소켓에서 처리됩니다")

            except Exception as websocket_ex:
                self.logger.error(f"❌ 조건검색 목록조회 웹소켓 요청 실패: {websocket_ex}")
                self.parent.condition_search_list = None
        except Exception as ex:
            self.logger.error(f"❌ 조건검색 목록조회 실패: {ex}")
            self.parent.condition_search_list = None
  
    def check_and_auto_execute_saved_condition(self):
        """저장된 조건검색식이 있는지 확인하고 자동 실행"""
        # 사용자가 수동으로 전략을 변경한 경우에는 자동 실행을 건너뜁니다.
        if self.is_manual_change:
            self.logger.debug("사용자 수동 변경으로 인해 저장된 조건검색식 자동 실행을 건너뜁니다.")
            self.is_manual_change = False # 플래그 초기화
            return False

        try:
            
            # .env에서 저장된 전략 확인
            config = EnvConfigParser()
            
            if config.has_option('SETTINGS', 'last_strategy'):
                last_strategy = config.get('SETTINGS', 'last_strategy')
                self.logger.debug(f"📋 저장된 전략 확인: {last_strategy}")
                
                # 저장된 전략이 조건검색식인지 확인 (조건검색 목록에 있는지 확인)
                if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                    condition_names = [condition['title'] for condition in self.parent.condition_search_list]
                    if last_strategy in condition_names:
                        self.logger.info(f"🔍 저장된 조건검색식 발견: {last_strategy}")
                        
                        # 콤보박스에서 해당 조건검색식 찾아서 선택 (시그널 발생 방지)
                        combo = self.parent.trading_tab.comboStg
                        index = combo.findText(last_strategy)
                        if index != -1:
                            combo.blockSignals(True)
                            try:
                                combo.setCurrentIndex(index)
                                self.logger.debug(f"✅ 저장된 조건검색식 UI 선택: {last_strategy}")
                            finally:
                                combo.blockSignals(False)
                            
                            # 자동 실행 (1초 후)
                            async def delayed_condition_search():
                                await asyncio.sleep(1.0)  # 1초 대기
                                # stg_changed를 직접 호출하여 로직 일원화
                                await self.parent.strategy_manager.stg_changed()

                            asyncio.create_task(delayed_condition_search())
                            self.logger.debug("🔍 저장된 조건검색식 자동 실행 예약 (1초 후)")
                            self.logger.debug("📋 조건검색식이 자동으로 실행되어 모니터링 종목에 추가됩니다")
                            return True # 함수를 종료하여 불필요한 폴백 로직 방지
                            # 여기서 함수를 종료하지 않고 계속 진행하여 다른 초기화 로직이 실행되도록 함
                
                # 통합 전략인 경우 모든 조건검색식 실행
                if last_strategy == "통합 전략":
                    self.logger.debug(f"🔍 저장된 통합 전략 발견: {last_strategy}")
                     # 콤보박스에서 통합 전략 찾기
                    index = self.parent.trading_tab.comboStg.findText(last_strategy)
                    if index >= 0:
                        # 통합 전략 선택
                        self.parent.trading_tab.comboStg.setCurrentIndex(index)
                        self.logger.debug(f"✅ 저장된 통합 전략 선택: {last_strategy}")
                        # 자동 실행 (1초 후)
                        async def delayed_integrated_search():
                            try:
                                await asyncio.sleep(1.5)
                                await self.parent.condition_search_manager.handle_integrated_condition_search()
                            except asyncio.CancelledError:
                                self.logger.debug("통합 조건검색 태스크 취소됨")
                            except Exception as e:
                                self.logger.error(f"통합 조건검색 실행 실패: {e}")
                        task = asyncio.create_task(delayed_integrated_search())
                        self.parent._delayed_search_task = task
                        self.logger.debug("🔍 저장된 통합 전략 자동 실행 예약 (1.5초 후)")
                        return True  # 함수 종료

                # 일반 조건검색식인 경우
                elif hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                    condition_names = [condition['title'] for condition in self.parent.condition_search_list]
                    if last_strategy in condition_names:
                        self.logger.debug(f"🔍 저장된 조건검색식 발견: {last_strategy}")
                        index = self.parent.trading_tab.comboStg.findText(last_strategy)
                        if index >= 0:
                            self.parent.trading_tab.comboStg.setCurrentIndex(index)
                            self.logger.debug(f"✅ 저장된 조건검색식 선택: {last_strategy}")
                            async def delayed_search():
                                await asyncio.sleep(1.5)
                                await self.parent.condition_search_manager.handle_condition_search()
                            asyncio.create_task(delayed_search())
                            self.logger.debug(f"🔍 저장된 조건검색식 자동 실행 예약 (1.5초 후)")
                            return True # 함수 종료
                else:
                    self.logger.debug(f"📋 저장된 전략이 조건검색식이 아닙니다: {last_strategy}")
                    self.logger.debug("📋 일반 투자전략이 선택되어 있습니다")
                    return False  # 조건검색식이 아님
            else:
                self.logger.debug("📋 저장된 전략이 없습니다")
                self.logger.debug("📋 투자전략 콤보박스에서 원하는 전략을 선택하세요")
                return False  # 저장된 전략이 없음
            
        except Exception as ex:
            self.logger.error(f"❌ 저장된 조건검색식 확인 및 자동 실행 실패: {ex}", exc_info=True)
            return False  # 오류 발생
    
    async def handle_integrated_condition_search(self):
        """통합 전략 실행: 모든 조건검색식 순차적으로 실행"""
        try:
            if not hasattr(self.parent, 'condition_search_list') or not self.parent.condition_search_list:
                self.logger.warning("⚠️ 조건검색 목록이 없어 통합 검색을 실행할 수 없습니다.")
                return

            self.logger.info(f"🔄 통합 조건검색 시작: {len(self.parent.condition_search_list)}개 조건식 실행")

            for condition in self.parent.condition_search_list:
                seq = condition.get('seq')
                name = condition.get('title')
                if seq and name:
                    self.logger.debug(f"  - 조건검색 실행: {name} (seq: {seq})")
                    # 각 조건검색을 순차적으로 실행하고, API 제한을 피하기 위해 약간의 지연을 둡니다.
                    await self.parent.start_condition_realtime(seq, name)
                    await asyncio.sleep(1) # API 요청 간격

            self.logger.debug("✅ 모든 조건검색식에 대한 실시간 모니터링이 시작되었습니다.")

        except Exception as ex:
            self.logger.error(f"❌ 통합 조건검색 실행 실패: {ex}")

    async def handle_condition_search(self):
        """조건검색 실행 (웹소켓 기반)"""
        try:
            if self.parent.trading_tab.comboStg.currentText():
                condition_name = self.parent.trading_tab.comboStg.currentText()
                condition_seq = next((item['seq'] for item in self.parent.condition_search_list if item['title'] == condition_name), None) # type: ignore
                if condition_seq is not None: # type: ignore
                    self.logger.info(f"🔍 조건검색 시작: {condition_name} (seq: {condition_seq})") # type: ignore
                    await self.parent.execute_condition_search(condition_seq, condition_name) # type: ignore
        except Exception as ex:
            self.logger.error(f"조건검색 실행 실패: {ex}", exc_info=True)

    async def stop_all_conditions(self):
        """모든 실시간 조건검색 중단"""
        try:
            if not hasattr(self.parent, 'condition_search_list') or not self.parent.condition_search_list:
                return

            self.logger.info("🛑 장 마감으로 인한 모든 실시간 조건검색 중단 요청")
            for condition in self.parent.condition_search_list:
                seq = condition.get('seq')
                if seq is not None:
                    await self.parent.stop_condition_realtime(seq)
                    await asyncio.sleep(0.2) # 약간의 딜레이
            
            self.logger.info("✅ 모든 실시간 조건검색이 중단되었습니다.")
        except Exception as ex:
            self.logger.error(f"❌ 모든 실시간 조건검색 중단 실패: {ex}")


