import logging

from config_manager import EnvConfigParser
from PyQt6.QtCore import pyqtSignal, QThread
from PyQt6.QtWidgets import QMessageBox, QApplication
from backtester import KiwoomBacktester


class BacktestWorker(QThread):
    """백테스팅을 비동기로 실행하기 위한 워커 스레드"""
    finished_signal = pyqtSignal(object, bool)  # backtester, success
    error_signal = pyqtSignal(str)

    def __init__(self, backtester, codes, start_date, end_date, strategy_name):
        super().__init__()
        self.backtester = backtester
        self.codes = codes
        self.start_date = start_date
        self.end_date = end_date
        self.strategy_name = strategy_name

    def run(self):
        try:
            # 백테스팅 실행 (시간이 오래 걸리는 작업)
            success = self.backtester.run_backtest(self.codes, self.start_date, self.end_date, self.strategy_name)
            self.finished_signal.emit(self.backtester, success)
        except Exception as e:
            self.error_signal.emit(str(e))

class BacktestManager:
    """백테스팅 관리 매니저"""
    
    def __init__(self, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
        self.worker = None  # 워커 스레드 참조 유지
    
    def load_backtest_strategies(self):
        """백테스팅 탭의 전략 콤보박스 로드"""
        try:
            self.parent.backtest_tab.bt_strategy_combo.clear()
            
            # 설정 파일에서 전략 로드
            config = EnvConfigParser()
            
            strategies = []
            if config.has_section('STRATEGIES'):
                # 섹션의 모든 키에 대한 값을 가져옴 (예: stg_1 = 급등주 -> '급등주')
                for key in config['STRATEGIES']:
                    strategies.append(config['STRATEGIES'][key])
            
            # 기본 전략 추가 (없으면)
            if '통합 전략' not in strategies:
                strategies.insert(0, '통합 전략')
            
            # 중복 제거 (순서 유지)
            strategies = list(dict.fromkeys(strategies))
            
            self.parent.backtest_tab.bt_strategy_combo.addItems(strategies)
            
            # 마지막 선택 전략 설정 (SETTINGS -> last_strategy)
            if config.has_option('SETTINGS', 'last_strategy'):
                last_strategy = config.get('SETTINGS', 'last_strategy')
                index = self.parent.backtest_tab.bt_strategy_combo.findText(last_strategy)
                if index >= 0:
                    self.parent.backtest_tab.bt_strategy_combo.setCurrentIndex(index)
            
            self.logger.debug(f"백테스팅 전략 로드 완료: {len(strategies)}개")
            
        except Exception as ex:
            self.logger.error(f"백테스팅 전략 로드 실패: {ex}")

    def load_db_period(self):
        """DB 데이터 기간 및 종목 목록 조회 및 UI 설정"""
        try:
            backtester = KiwoomBacktester(db_path='stock_data.db')
            start_date, end_date = backtester.get_db_data_range()
            
            if start_date and end_date:
                self.parent.backtest_tab.bt_start_date.setText(start_date)
                self.parent.backtest_tab.bt_end_date.setText(end_date)
                self.logger.info(f"DB 데이터 기간 로드: {start_date} ~ {end_date}")
            else:
                self.logger.warning("DB에 데이터가 없거나 기간을 조회할 수 없습니다.")
            
            # 종목 목록 로드
            codes = backtester.get_all_stock_codes()
            self.parent.backtest_tab.bt_stock_combo.clear()
            self.parent.backtest_tab.bt_stock_combo.addItem("전체 종목")
            if codes:
                self.parent.backtest_tab.bt_stock_combo.addItems(codes)
                self.logger.info(f"DB 종목 목록 로드 완료: {len(codes)}개")
                
        except Exception as ex:
            self.logger.error(f"DB 정보 로드 실패: {ex}")
    
    def run_backtest(self):
        """백테스팅 실행 (비동기)"""
        try:
            # 중복 실행 방지
            if self.worker and self.worker.isRunning():
                self.logger.warning("이미 백테스팅이 진행 중입니다.")
                return

            # 1. KiwoomBacktester 인스턴스 생성 (기간 조회를 위해 먼저 생성)
            backtester = KiwoomBacktester(db_path='stock_data.db')
    
            # 2. DB에서 실제 데이터 기간 자동 조회
            start_date, end_date = backtester.get_db_data_range()
            if not start_date or not end_date: # type: ignore
                self.logger.warning("DB에서 백테스팅을 위한 데이터 기간을 찾을 수 없습니다.")
                return

            # 3. UI에 조회된 기간 설정 및 파라미터 가져오기
            self.parent.backtest_tab.bt_start_date.setText(start_date)
            self.parent.backtest_tab.bt_end_date.setText(end_date)
            initial_cash = int(self.parent.backtest_tab.bt_initial_cash.text())
            strategy_name = self.parent.backtest_tab.bt_strategy_combo.currentText()
            backtester.initial_cash = initial_cash # 초기 자금 설정

            if not all([start_date, end_date, strategy_name]): # type: ignore
                self.logger.warning("백테스팅 입력 오류: 시작일, 종료일, 투자 전략을 모두 선택해주세요.")
                return

            self.logger.info(f"백테스팅 시작 요청: {strategy_name} ({start_date} ~ {end_date})") # type: ignore
            self.parent.backtest_tab.bt_results_text.clear()
            self.parent.backtest_tab.bt_results_text.append(f"백테스팅을 시작합니다: {strategy_name}\n")
            self.parent.backtest_tab.bt_results_text.append("데이터 로딩 및 분석 중... 잠시만 기다려주세요.\n(데이터 양에 따라 시간이 소요될 수 있습니다)\n")
            
            # UI 비활성화
            self.parent.backtest_tab.bt_run_button.setEnabled(False)
            self.parent.backtest_tab.bt_run_button.setText("진행 중...")
            QApplication.processEvents()

            # 4. 백테스팅 대상 종목 가져오기
            selected_stock = self.parent.backtest_tab.bt_stock_combo.currentText()
            
            if selected_stock == "전체 종목":
                codes = backtester.get_all_stock_codes()
            else:
                codes = [selected_stock]
                
            if not codes:
                self.logger.warning("백테스팅 오류: 대상 종목이 없습니다.")
                self.parent.backtest_tab.bt_run_button.setEnabled(True)
                self.parent.backtest_tab.bt_run_button.setText("백테스팅 실행")
                return

            self.parent.backtest_tab.bt_results_text.append(f"대상 종목: {len(codes)}개 종목\n")
            QApplication.processEvents()
            
            # 5. 워커 스레드 생성 및 실행
            self.worker = BacktestWorker(backtester, codes, start_date, end_date, strategy_name)
            self.worker.finished_signal.connect(self.on_backtest_finished)
            self.worker.error_signal.connect(self.on_backtest_error)
            self.worker.start()

        except Exception as ex:
            self.logger.error(f"백테스팅 실행 준비 실패: {ex}")
            self.parent.backtest_tab.bt_run_button.setEnabled(True)
            self.parent.backtest_tab.bt_run_button.setText("백테스팅 실행")
            # QMessageBox.critical(self.parent, "백테스팅 오류", f"백테스팅 실행 중 오류가 발생했습니다:\n{ex}")

    def on_backtest_finished(self, backtester, success):
        """백테스팅 완료 처리"""
        try:
            strategy_name = self.worker.strategy_name
            
            # UI 복구
            self.parent.backtest_tab.bt_run_button.setEnabled(True)
            self.parent.backtest_tab.bt_run_button.setText("백테스팅 실행")
            
            if success and strategy_name in backtester.results:
                result = backtester.results[strategy_name]
                summary = (
                    f"총 수익률: {result['total_return']:.2f}%\n"
                    f"최종 자산: {result['final_value']:,.0f}원\n"
                    f"승률: {result['win_rate']:.2f}%\n"
                    f"총 거래 수: {result['total_trades']}\n"
                    f"최대 낙폭: {result['max_drawdown']:.2f}%"
                )
                self.parent.backtest_tab.bt_results_text.append("\n=== 백테스팅 결과 ===\n" + summary)
                self.parent.backtest_tab.bt_results_text.append("\n=== 백테스팅 결과 ===\n" + summary)
                
                # 차트 위젯에 결과 그리기 (인터랙티브 차트)
                backtester.plot_results(strategy_name, target_widget=self.parent.backtest_tab.bt_chart_widget)
                
                # 엑셀 내보내기 (선택 사항)
                backtester.export_results(strategy_name)
                self.logger.info("백테스팅 완료 및 결과 표시 성공")

            else:
                self.parent.backtest_tab.bt_results_text.append("\n백테스팅 실행에 실패했거나 결과가 없습니다.")
                self.logger.warning("백테스팅 실패 또는 결과 없음")
                self.logger.warning("백테스팅 실패 또는 결과 없음")
                
        except Exception as ex:
            self.logger.error(f"백테스팅 결과 처리 중 오류: {ex}")
            self.parent.backtest_tab.bt_results_text.append(f"\n결과 처리 중 오류 발생: {ex}")

    def on_backtest_error(self, error_msg):
        """백테스팅 에러 처리"""
        self.logger.error(f"백테스팅 중 오류 발생: {error_msg}")
        self.parent.backtest_tab.bt_results_text.append(f"\n❌ 백테스팅 중 오류 발생: {error_msg}")
        
        # UI 복구
        self.parent.backtest_tab.bt_run_button.setEnabled(True)
        self.parent.backtest_tab.bt_run_button.setText("백테스팅 실행")

