import logging
from datetime import datetime

from PyQt6.QtCore import QTimer, QThread
from ml_trainer import MLTrainingWorker


class MLManager:
    """머신러닝 학습 관리자"""
    
    def __init__(self, parent):
        self.parent = parent
        self.logger = logging.getLogger(self.__class__.__name__)
        self.trainer_thread = None
        
        # 학습 스케줄러 타이머
        self.schedule_timer = QTimer(parent)
        self.schedule_timer.timeout.connect(self._check_schedule)
        self.schedule_timer.start(60000) # 1분마다 체크
        
        self.logger.info("🤖 ML 매니저 초기화 완료 (스케줄러 동작 중)")
        
    def _check_schedule(self):
        """정해진 시간에 학습 시작"""
        now = datetime.now()
        current_time_str = now.strftime('%H:%M')
        
        # 1. 점심시간 학습 (12:30)
        if current_time_str == '12:30':
            self.logger.info("⏰ 점심시간 도래: AI 모델 중간 학습(Light Update)을 시작합니다.")
            self.start_training()
            
        # 2. 장 마감 후 학습 (15:30)
        if current_time_str == '15:30':
            self.logger.info("⏰ 장 마감: AI 모델 정밀 학습(Deep Training)을 시작합니다.")
            self.start_training()
            
    def start_training(self):
        """학습 스레드 시작"""
        try:
            if self.trainer_thread is not None and self.trainer_thread.isRunning():
                self.logger.warning("⚠️ 이미 학습이 진행 중입니다.")
                return

            self.logger.info("🚀 ML 학습 스레드 시작 요청...")
            
            # 워커 생성
            self.trainer_thread = MLTrainingWorker()
            
            # 시그널 연결
            self.trainer_thread.progress_signal.connect(self._on_progress)
            self.trainer_thread.finished_signal.connect(self._on_finished)
            # 스레드 종료 시 메모리 정리
            self.trainer_thread.finished.connect(self.trainer_thread.deleteLater) # QThread 기본 시그널
            
            # 시작
            self.trainer_thread.start()
            
        except Exception as ex:
            self.logger.error(f"❌ ML 학습 시작 실패: {ex}")
            
    def _on_progress(self, msg):
        """학습 진행 상황 로그 출력"""
        self.logger.info(f"{msg}")

    def _on_finished(self, success, msg):
        """학습 완료 처리"""
        if success:
            self.logger.info(f"✨ {msg}")
            if hasattr(self.parent, 'trading_tab'):
                 self.parent.trading_tab.terminalOutput.append(f"<span style='color: #00FF00; font-weight: bold;'>[{datetime.now().strftime('%H:%M:%S')}] {msg}</span>")
            
            # 여기서 모델 파일 로드 (추후 전략에서 자동 감지하도록 구현)
        else:
            self.logger.warning(f"⚠️ {msg}")
            if hasattr(self.parent, 'trading_tab'):
                 self.parent.trading_tab.terminalOutput.append(f"<span style='color: #FF0000;'>[{datetime.now().strftime('%H:%M:%S')}] {msg}</span>")
        
        # 참조 제거
        self.trainer_thread = None