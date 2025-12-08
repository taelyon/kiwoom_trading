@echo off
chcp 65001
echo 가상환경을 활성화합니다...
call .venv\Scripts\activate.bat

echo PyInstaller를 실행하여 빌드를 시작합니다...
pyinstaller --clean -y stock_trader.spec

echo settings.ini 파일을 dist\stock_trader 폴더로 복사합니다...
copy /Y settings.ini dist\stock_trader\settings.ini

echo 작업이 완료되었습니다.
pause
