"""
UI 매니저 모듈 (호환성 re-export)

각 매니저 클래스를 역할별 모듈로 분리하였습니다:
- login_manager.py: LoginHandler (로그인/연결 관리)
- data_manager.py: DataManager (데이터 관리)
- monitoring_manager.py: MonitoringManager (종목 모니터링)
- strategy_manager.py: StrategyManager (전략 설정)
- trading_manager.py: TradingManager (매매 관리)
- account_manager.py: AccountManager (계좌 관리)
- condition_manager.py: ConditionSearchManager (조건검색)
- ml_manager.py: MLManager (머신러닝)

기존 코드에서 `from ui_managers import ...`로 임포트하던 것을 그대로 유지합니다.
"""

from login_manager import LoginHandler
from data_manager import DataManager
from monitoring_manager import MonitoringManager
from strategy_manager import StrategyManager
from trading_manager import TradingManager
from account_manager import AccountManager
from condition_manager import ConditionSearchManager
from ml_manager import MLManager

__all__ = [
    'LoginHandler',
    'DataManager',
    'MonitoringManager',
    'StrategyManager',
    'TradingManager',
    'AccountManager',
    'ConditionSearchManager',
    'MLManager',
]
