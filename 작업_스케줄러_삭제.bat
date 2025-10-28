@echo off
chcp 65001 >nul 2>&1
setlocal

echo.
echo ==========================================
echo  Stock Trader 자동 실행 설정 삭제
echo ==========================================
echo.

REM 작업 존재 확인
schtasks /Query /TN "StockTrader_Auto" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [알림] 등록된 작업이 없습니다.
    echo.
    pause
    exit /b 0
)

echo 작업 스케줄러에서 "StockTrader_Auto" 작업을 삭제합니다.
echo.
echo 계속하시겠습니까? (Y/N)
set /p confirm=

if /i "%confirm%" NEQ "Y" (
    echo 취소되었습니다.
    pause
    exit /b 0
)

echo.
echo 삭제 중...

schtasks /Delete /TN "StockTrader_Auto" /F

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ==========================================
    echo  삭제 성공!
    echo ==========================================
    echo.
    echo ✅ "StockTrader_Auto" 작업이 삭제되었습니다.
    echo.
) else (
    echo.
    echo ==========================================
    echo  삭제 실패
    echo ==========================================
    echo.
    echo [오류] 작업 삭제에 실패했습니다.
    echo 관리자 권한으로 실행했는지 확인하세요.
    echo.
)

pause

