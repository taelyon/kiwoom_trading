import logging
from utils import create_fire_and_forget_task
import asyncio
import ast
import json

from config_manager import EnvConfigParser
from strategy import KiwoomStrategy


class StrategyManager:
    """전략 로드/저장 관리 매니저"""
    
    def __init__(self, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
    
    def load_strategy_combos(self):
        """.env 값 로드"""
        try:
            config = EnvConfigParser()
            
            # 시그널을 잠시 끊어 중복 로드를 방지
            self.parent.trading_tab.comboStg.blockSignals(True)
            try:
                # 투자전략 콤보박스 로드
                self.parent.trading_tab.comboStg.clear()
                if config.has_section('STRATEGIES'):
                    for key, value in config.items('STRATEGIES'):
                        if key.startswith('stg_') or key == 'stg_integrated':
                            self.parent.trading_tab.comboStg.addItem(value)
                
                # 기본 전략 설정
                if config.has_option('SETTINGS', 'last_strategy'):
                    last_strategy = config.get('SETTINGS', 'last_strategy')
                    index = self.parent.trading_tab.comboStg.findText(last_strategy)
                    if index >= 0:
                        self.parent.trading_tab.comboStg.setCurrentIndex(index)
                        self.logger.debug(f"✅ 저장된 투자전략 복원: {last_strategy}")
                    else:
                        self.logger.warning(f"⚠️ 저장된 투자전략을 찾을 수 없습니다: {last_strategy}")
                else:
                    self.logger.debug("저장된 투자전략이 없습니다. 기본 전략을 사용합니다.")
            finally:
                # 시그널 다시 연결
                self.parent.trading_tab.comboStg.blockSignals(False)
                    
            # 매수전략 콤보박스 로드
            self.load_buy_strategies() # type: ignore
            
            # 매도전략 콤보박스 로드
            self.load_sell_strategies() # type: ignore
            
            # 초기 전략 내용 로드
            self.load_initial_strategy_content()
            
            self.logger.debug("투자전략 콤보박스 로드 완료")
            
        except Exception as ex:
            self.logger.error(f"전략 콤보박스 로드 실패: {ex}")
    
    def load_buy_strategies(self):
        """매수전략 콤보박스 로드"""
        self._load_strategy_list(self.parent.trading_tab.comboBuyStg, 'buy_stg_', 'buy')
    
    def load_sell_strategies(self):
        """매도전략 콤보박스 로드"""
        self._load_strategy_list(self.parent.trading_tab.comboSellStg, 'sell_stg_', 'sell')
    
    def _load_strategy_list(self, combo_widget, key_prefix, strategy_type):
        """전략 목록을 콤보박스에 로드"""
        try:
            combo_widget.blockSignals(True)  # 시그널 차단
            combo_widget.clear()
            
            config = EnvConfigParser()
            
            # 현재 선택된 투자전략 가져오기
            current_strategy = self.parent.trading_tab.comboStg.currentText()
            if not current_strategy:
                self.logger.warning("선택된 투자전략이 없습니다")
                return
            
            strategies = []

            if current_strategy == "통합 전략":
                # 급등주 + 갭상승 병합 로드 (숫자순)
                merge_sections = []
                if config.has_section('급등주'):
                    merge_sections.append('급등주')
                if config.has_section('갭상승'):
                    merge_sections.append('갭상승')

                for section in merge_sections:
                    # key_prefix로 필터링 후 숫자순 정렬
                    items = [(k, v) for k, v in config.items(section) if k.startswith(key_prefix)]
                    items.sort(key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 999)
                    for key, value in items:
                        try:
                            strategy_data = json.loads(value)
                            name = strategy_data.get('name', key)
                            # 구분을 위해 섹션 라벨 추가
                            display_name = f"[{section}] {name}"
                            strategies.append((f"{section}.{key}", display_name))
                        except json.JSONDecodeError:
                            self.logger.warning(f"전략 파싱 실패: {section}.{key}")
            else:
                # 해당 전략 섹션 확인
                if not config.has_section(current_strategy):
                    self.logger.warning(f".env에 [{current_strategy}] 섹션이 없습니다")
                    return
                
                # 전략 목록 추출
                for key in config[current_strategy]:
                    if key.startswith(key_prefix):
                        try:
                            strategy_data = json.loads(config[current_strategy][key])
                            strategies.append((key, strategy_data.get('name', key)))
                        except json.JSONDecodeError:
                            self.logger.warning(f"전략 파싱 실패: {key}")
            
            # 콤보박스에 추가
            for key, name in strategies:
                combo_widget.addItem(name, key)
            
            self.logger.debug(f"{strategy_type} 전략 {len(strategies)}개 로드 완료")
            
        except Exception as ex:
            self.logger.error(f"전략 목록 로드 실패 ({strategy_type}): {ex}")
        finally:
            combo_widget.blockSignals(False)  # 시그널 복구
    
    def save_current_strategy(self):
        """현재 선택된 투자전략을 .env에 저장"""
        try:
            current_strategy = self.parent.trading_tab.comboStg.currentText()
            if not current_strategy:
                self.logger.debug("저장할 투자전략이 없습니다")
                return
            
            config = EnvConfigParser()
            
            # [Strategy] 섹션 대신 [SETTINGS].last_strategy에 통합 저장
            if not config.has_section('SETTINGS'):
                config.add_section('SETTINGS')
            config.set('SETTINGS', 'last_strategy', current_strategy)
            
            with open('.env', 'w', encoding='utf-8') as f:
                config.write(f)
            
            self.logger.debug(f"✅ 현재 투자전략 저장(SETTINGS.last_strategy): {current_strategy}")
            
        except Exception as ex:
            self.logger.error(f"투자전략 저장 실패: {ex}")
    
    def load_initial_strategy_content(self):
        """초기 전략 내용을 텍스트박스에 로드"""
        try:
            # 매수전략 초기 내용 로드 # type: ignore
            if self.parent.trading_tab.comboBuyStg.count() > 0:
                current_buy_strategy = self.parent.trading_tab.comboBuyStg.currentText()
                self.load_strategy_content(current_buy_strategy, 'buy')
            
            # 매도전략 초기 내용 로드
            if self.parent.trading_tab.comboSellStg.count() > 0:
                current_sell_strategy = self.parent.trading_tab.comboSellStg.currentText()
                self.load_strategy_content(current_sell_strategy, 'sell')
                
        except Exception as ex:
            self.logger.error(f"초기 전략 내용 로드 실패: {ex}")
    
    def load_strategy_content(self, strategy_name, strategy_type):
        """전략 내용을 텍스트 위젯에 로드"""
        try:
            config = EnvConfigParser()
            
            current_strategy = self.parent.trading_tab.comboStg.currentText()

            target_section = current_strategy
            target_key = None
            display_name = strategy_name

            # 통합 전략: 콤보 표시명은 "[섹션] 이름" → 섹션/이름 분리
            if current_strategy == "통합 전략" and strategy_name.startswith('['):
                try:
                    end_idx = strategy_name.find(']')
                    section_label = strategy_name[1:end_idx]
                    display_name = strategy_name[end_idx+2:]
                    if config.has_section(section_label):
                        target_section = section_label
                except Exception:
                    pass

            if not config.has_section(target_section):
                return

            # 전략 키 찾기 (섹션 내)
            for key, value in config.items(target_section):
                try:
                    strategy_data = eval(value)
                    if isinstance(strategy_data, dict) and strategy_data.get('name') == display_name:
                        if strategy_type == 'buy' and key.startswith('buy_stg_'):
                            target_key = key
                            break
                        elif strategy_type == 'sell' and key.startswith('sell_stg_'):
                            target_key = key
                            break
                except Exception:
                    continue

            if target_key:
                strategy_data = eval(config.get(target_section, target_key))
                content = strategy_data.get('content', '')
                
                # 텍스트 위젯에 표시
                if strategy_type == 'buy':
                    self.parent.trading_tab.buystgInputWidget.setPlainText(content)
                elif strategy_type == 'sell':
                    self.parent.trading_tab.sellstgInputWidget.setPlainText(content)
                    
        except Exception as ex:
            self.logger.error(f"전략 내용 로드 실패: {ex}")
    
    async def _clear_monitoring_list(self):
        """모니터링 리스트와 관련된 상태를 초기화합니다. (단, 보유 종목은 제외)"""
        self.logger.info("🔄 투자 전략 변경으로 인해 기존 모니터링 목록을 초기화합니다.")
        
        # 1. 현재 보유 중인 종목 코드를 가져옵니다.
        holding_codes = set()
        if hasattr(self.parent, 'trading_tab') and hasattr(self.parent.trading_tab, 'boughtBox'):
            for i in range(self.parent.trading_tab.boughtBox.count()):
                item = self.parent.trading_tab.boughtBox.item(i)
                if item:
                    holding_codes.add(item.text().split()[0])
        self.logger.debug(f"보유 종목은 모니터링에서 제외하지 않습니다: {list(holding_codes)}")

        # 2. 서버에 등록된 *모든* 조건검색을 해제 시도 (상태 불일치 해결)
        if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
            all_seqs = [c['seq'] for c in self.parent.condition_search_list]
            self.logger.debug(f"🔍 모든 조건검색 해제 시도: {all_seqs}")
            for seq in all_seqs:
                await self.parent.stop_condition_realtime(seq)
        
        # 3. 클라이언트의 활성 목록도 비웁니다.
        if hasattr(self.parent, 'active_realtime_conditions'):
            self.parent.active_realtime_conditions.clear()
        self.logger.debug("✅ 모든 실시간 조건검색 모니터링 중단 완료.")

        # 4. 모니터링 리스트 박스에서 보유 종목을 제외하고 비우기
        monitoring_box = self.parent.trading_tab.monitoringBox
        for i in range(monitoring_box.count() - 1, -1, -1):
            item = monitoring_box.item(i)
            if item and item.text().split()[0] not in holding_codes:
                monitoring_box.takeItem(i)
        
        # 5. 모니터링 종목이 비워졌으므로 차트 업데이트 주기 조절 (타이머 중지)
        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
            self.parent.chart_cache.update_chart_update_interval()

        # 6. 차트 캐시에서 보유 종목이 아닌 것들만 제거
        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
            codes_to_remove = [code for code in self.parent.chart_cache.cache.keys() if code not in holding_codes]
            for code in codes_to_remove:
                self.parent.chart_cache.remove_monitoring_stock(code)
            self.logger.debug(f"차트 캐시에서 {len(codes_to_remove)}개 종목 제거 완료.")

    async def stg_changed(self):
        """전략 변경 이벤트 핸들러 (비동기)"""
        try:
            # 수동 변경 플래그 설정
            self.parent.condition_search_manager.is_manual_change = True

            strategy_name = self.parent.trading_tab.comboStg.currentText()
            self.logger.debug(f"투자 전략 변경: {strategy_name}")

            # 초기 로딩 중에는 저장하지 않음
            if self.parent.is_loading_strategy:
                self.logger.debug("초기화 중... 전략 저장을 건너뜁니다.")
                return
            
            # 기존 모니터링 초기화 (비동기 호출)
            await self._clear_monitoring_list()
            await asyncio.sleep(1.0)  # 0.5초 대기

            # 현재 선택된 전략을 .env에 저장
            self.save_current_strategy()
            
            # 투자전략 변경 시 매수/매도 전략 목록을 항상 새로고침합니다.
            self.load_buy_strategies()
            self.load_sell_strategies()
            
            # 변경된 전략의 첫 번째 매수/매도 전략 내용 자동 로드
            self.load_initial_strategy_content()

            # 조건검색식인지 확인 (조건검색 목록에 있는지 확인)
            if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                condition_names = [condition['title'] for condition in self.parent.condition_search_list]
                if strategy_name in condition_names:
                    # 조건검색식 선택 시 바로 실행 (비동기)
                    try:
                        create_fire_and_forget_task(self.parent.condition_search_manager.handle_condition_search())
                    except RuntimeError:
                        self.logger.warning("⚠️ 이벤트 루프가 없어 조건검색을 실행할 수 없습니다")
            
            # 통합 전략인 경우 모든 조건검색식 실행
            if strategy_name == "통합 전략":
                if hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                    self.logger.debug("🔍 통합 전략 실행: 모든 조건검색식 적용 (ConditionSearchManager)")
                    try:
                        create_fire_and_forget_task(self.parent.condition_search_manager.handle_integrated_condition_search())
                    except RuntimeError:
                        logging.warning("⚠️ 이벤트 루프가 없어 통합 전략을 실행할 수 없습니다")
            
        except Exception as ex:
            self.logger.error(f"전략 변경 실패: {ex}")
    
    def buy_stg_changed(self):
        """매수 전략 변경 이벤트 핸들러"""
        try:
            strategy_name = self.parent.trading_tab.comboBuyStg.currentText()
            if not strategy_name:  # 빈 전략 이름 무시
                return
            self.logger.debug(f"매수 전략 변경: {strategy_name}")
            
            # 매수 전략 내용을 텍스트 위젯에 표시
            self.load_strategy_content(strategy_name, 'buy')
            
        except Exception as ex:
            self.logger.error(f"매수 전략 변경 실패: {ex}")
    
    def sell_stg_changed(self):
        """매도 전략 변경 이벤트 핸들러"""
        try:
            strategy_name = self.parent.trading_tab.comboSellStg.currentText()
            if not strategy_name:  # 빈 전략 이름 무시
                return
            self.logger.debug(f"매도 전략 변경: {strategy_name}")
            
            # 매도 전략 내용을 텍스트 위젯에 표시
            self.load_strategy_content(strategy_name, 'sell')
            
        except Exception as ex:
            self.logger.error(f"매도 전략 변경 실패: {ex}")
    
    def _save_strategy(self, text_widget, combo_widget, key_prefix, strategy_type):
        """전략 저장 (공통 로직)
        
        Args:
            text_widget: 전략 내용이 있는 텍스트 위젯
            combo_widget: 전략 선택 콤보박스 위젯
            key_prefix: 전략 키 접두사 ('buy_stg_' 또는 'sell_stg_')
            strategy_type: 전략 타입 ('매수' 또는 '매도')
        """
        try:
            strategy_text = text_widget.toPlainText()
            current_strategy = self.parent.trading_tab.comboStg.currentText()
            current_strategy_name = combo_widget.currentText()
            key_from_combobox = combo_widget.currentData()

            # .env 파일 업데이트
            config = EnvConfigParser()

            target_section = current_strategy
            target_key = key_from_combobox

            # "통합 전략"일 경우, 실제 섹션과 키를 찾아서 업데이트
            if current_strategy == "통합 전략":
                if '.' in key_from_combobox:
                    section, key = key_from_combobox.split('.', 1)
                    target_section = section
                    target_key = key
                else:
                    self.logger.error(f"통합 전략 저장 오류: 잘못된 키 형식 - {key_from_combobox}")
                    return

            if not config.has_section(target_section):
                self.logger.error(f"{strategy_type} 전략 저장 실패: 섹션을 찾을 수 없음 - [{target_section}]")
                return

            if not config.has_option(target_section, target_key):
                logging.error(f"{strategy_type} 전략 저장 실패: 키를 찾을 수 없음 - {target_key}")
                return

            # 기존 전략 데이터 로드 및 수정
            try:
                strategy_json_str = config.get(target_section, target_key)
                strategy_data = ast.literal_eval(strategy_json_str) # json.loads 대신 ast.literal_eval 사용
                strategy_data['content'] = strategy_text
                # json.dumps를 사용하여 문자열로 변환
                config.set(target_section, target_key, json.dumps(strategy_data, ensure_ascii=False))
            except (ValueError, SyntaxError, KeyError) as e:
                self.logger.error(f"전략 데이터 파싱 또는 수정 실패: {e}")
                return

            # 파일 저장
            with open('.env', 'w', encoding='utf-8') as configfile:
                config.write(configfile)

            self.logger.debug(f"{strategy_type} 전략 '{current_strategy_name}'이 저장되었습니다.")

            # 전략 즉시 반영: KiwoomStrategy 객체의 설정을 다시 로드
            if hasattr(self.parent, 'objstg') and self.parent.objstg:
                self.parent.objstg.load_strategy_config()
                self.logger.info(f"✅ {strategy_type} 전략이 즉시 반영되었습니다.")

        except Exception as ex:
            self.logger.error(f"{strategy_type} 전략 저장 실패: {ex}")
    
    def _save_strategy_legacy(self, text_widget, combo_widget, key_prefix, strategy_type):
        """전략 저장 (레거시 - eval 사용)"""
        try:
            strategy_text = text_widget.toPlainText()
            current_strategy_name = combo_widget.currentText()
            key_from_combobox = combo_widget.currentData()

            config = EnvConfigParser()

            strategy_data = eval(config.get(current_strategy_name, key_from_combobox))
            strategy_data['content'] = strategy_text
            config.set(current_strategy_name, key_from_combobox, str(strategy_data))

            with open('.env', 'w', encoding='utf-8') as configfile:
                config.write(configfile)
        except Exception as ex:
            self.logger.error(f"{strategy_type} 전략 저장 실패 (레거시): {ex}")

    def save_buystrategy(self):
        """매수 전략 저장"""
        self._save_strategy(self.parent.trading_tab.buystgInputWidget, self.parent.trading_tab.comboBuyStg, 'buy_stg_', '매수')
    
    def save_sellstrategy(self):
        """매도 전략 저장"""
        self._save_strategy(self.parent.trading_tab.sellstgInputWidget, self.parent.trading_tab.comboSellStg, 'sell_stg_', '매도')

