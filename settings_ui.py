import logging
import os
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QPushButton, QTableWidget, QTableWidgetItem, 
                             QHeaderView, QMessageBox)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from config_manager import EnvConfigParser

class SettingsTabWidget(QWidget):
    """환경 설정 탭 위젯 (.env 관리)"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self.logger = logging.getLogger(self.__class__.__name__)
        self.config = EnvConfigParser()
        self.init_ui()
        
    def init_ui(self):
        layout = QVBoxLayout()
        
        # 안내 문구
        info_label = QLabel("⚙️ 앱 환경설정 (.env 파일 실시간 관리)")
        info_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        layout.addWidget(info_label)
        
        desc_label = QLabel("값(Value) 컬럼을 더블클릭하여 수정 후 저장 버튼을 누르세요. 일부 설정은 프로그램 재시작 후 적용됩니다.")
        desc_label.setStyleSheet("color: #666666;")
        layout.addWidget(desc_label)
        
        # 테이블 위젯
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["섹션(Prefix)", "전체 키(Key)", "값(Value)"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        # 번갈아가며 색상 표시 (가독성 향상)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)
        
        # 버튼 영역
        btn_layout = QHBoxLayout()
        self.reload_btn = QPushButton("🔄 설정 새로고침")
        self.reload_btn.setFixedHeight(35)
        self.reload_btn.setStyleSheet("padding: 5px 15px;")
        self.reload_btn.clicked.connect(self.load_settings)
        
        self.save_btn = QPushButton("💾 설정 저장")
        self.save_btn.setFixedHeight(35)
        self.save_btn.clicked.connect(self.save_settings)
        self.save_btn.setStyleSheet("background-color: #2e8b57; color: white; font-weight: bold; padding: 5px 15px;")
        
        btn_layout.addStretch()
        btn_layout.addWidget(self.reload_btn)
        btn_layout.addWidget(self.save_btn)
        layout.addLayout(btn_layout)
        
        self.setLayout(layout)
        self.load_settings()
        
    def load_settings(self):
        """.env 설정값들을 테이블에 로드"""
        # 디스크의 .env 파일이 외부에서 변경되었을 수 있으므로 메모리 최신화
        self.config.reload()
        
        self.table.setRowCount(0)
        row = 0
        
        # 민감한 정보를 마스킹할 키워드들
        sensitive_keywords = ['APPKEY', 'SECRETKEY', 'WEBHOOK', 'TOKEN', 'PASSWORD']
        
        # self.config._data 에 있는 값들을 표시 (알파벳 순 정렬)
        for k, v in sorted(self.config._data.items()):
            self.table.insertRow(row)
            
            # 키 분리 로직 (가장 앞의 _ 기준)
            parts = k.split('_', 1)
            section = parts[0] if len(parts) > 1 else "기타"
            
            # 섹션 셀 (읽기 전용)
            item_section = QTableWidgetItem(section)
            item_section.setFlags(item_section.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_section.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 0, item_section)
            
            # 키 셀 (읽기 전용)
            item_key = QTableWidgetItem(k)
            item_key.setFlags(item_key.flags() & ~Qt.ItemFlag.ItemIsEditable)
            font = item_key.font()
            font.setBold(True)
            item_key.setFont(font)
            self.table.setItem(row, 1, item_key)
            
            # 값 셀 (편집 가능)
            # 민감한 정보는 화면에 '********'로 표시하되, 실제 데이터는 Qt.ItemDataRole.UserRole에 저장
            is_sensitive = any(kw in k.upper() for kw in sensitive_keywords)
            display_value = "********" if is_sensitive and v else str(v)
            
            item_value = QTableWidgetItem(display_value)
            item_value.setData(Qt.ItemDataRole.UserRole, str(v)) # 원본 데이터 숨김 저장
            self.table.setItem(row, 2, item_value)
            
            row += 1
            
    def save_settings(self):
        """테이블의 값들을 .env에 저장"""
        try:
            for row in range(self.table.rowCount()):
                k = self.table.item(row, 1).text()
                
                item_value = self.table.item(row, 2)
                display_v = item_value.text()
                original_v = item_value.data(Qt.ItemDataRole.UserRole)
                
                # 만약 화면에 여전히 '********' 이면 (수정하지 않았다면) 원본 데이터를 사용
                if display_v == "********" and original_v is not None:
                    v = original_v
                else:
                    v = display_v
                
                # config에 업데이트 (메모리)
                self.config._data[k] = v
                os.environ[k] = v
                
            # 디스크(.env)에 저장 (수정된 save() 메서드 활용)
            self.config.save()
            
            QMessageBox.information(self, "저장 완료", "설정이 성공적으로 저장되었습니다.\n(일부 설정은 재시작 후 적용될 수 있습니다)")
            self.logger.info("✅ 환경설정(.env)이 UI를 통해 저장되었습니다.")
            
        except Exception as e:
            self.logger.error(f"설정 저장 중 오류: {e}")
            QMessageBox.critical(self, "저장 실패", f"설정 저장 중 오류가 발생했습니다:\n{e}")
