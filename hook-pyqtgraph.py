# -*- coding: utf-8 -*-
"""
PyInstaller hook for pyqtgraph and PyQt6

1. pyqtgraph.opengl 모듈을 제외하여 OpenGL 의존성 제거
2. PyQt5를 명시적으로 제외하여 PyQt6와의 충돌 방지
3. PyQt6 및 TA-Lib 서브모듈을 포함하여 의존성 문제 해결
PyInstaller hook for pyqtgraph
pyqtgraph.opengl 모듈을 제외하여 OpenGL 의존성 제거
"""

from PyInstaller.utils.hooks import collect_submodules

# pyqtgraph의 모든 서브모듈을 수집하되, opengl은 제외합니다.
hiddenimports = [mod for mod in collect_submodules('pyqtgraph') if 'opengl' not in mod.lower()]

# PyQt6와의 충돌을 방지하기 위해 PyQt5를 명시적으로 제외합니다.
excludes = [
    'PyQt5',
    'PyQt5.QtCore',
    'PyQt5.QtGui',
    'PyQt5.QtWidgets',
]

# PyQt6를 명시적으로 포함하여 PyInstaller가 의존성을 찾도록 돕습니다.
try:
    from PyInstaller.utils.hooks import collect_submodules
    hiddenimports.extend(collect_submodules('PyQt6'))
    # TA-Lib의 숨겨진 모듈(stream 등)을 포함하여 ModuleNotFoundError 방지
    hiddenimports.extend(collect_submodules('talib'))
except ImportError:
    # PyInstaller 버전이 낮아 collect_submodules가 없을 경우를 대비
    pass
