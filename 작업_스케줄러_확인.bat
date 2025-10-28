@echo off
chcp 65001 >nul 2>&1
setlocal

echo.
echo ==========================================
echo  Stock Trader 자동 실행 설정 확인
echo ==========================================
echo.

REM 작업 존재 확인
schtasks /Query /TN "StockTrader_Auto" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [알림] 등록된 작업이 없습니다.
    echo.
    echo "작업_스케줄러_등록.bat"를 실행하여 등록하세요.
    echo.
    pause
    exit /b 0
)

echo 📋 등록된 작업 정보:
echo.
schtasks /Query /TN "StockTrader_Auto" /V /FO LIST

echo.
echo ==========================================
echo  작업 관리
echo ==========================================
echo.
echo 1. 작업 스케줄러 열기 (GUI)
echo 2. 수동 테스트 실행
echo 3. 작업 비활성화
echo 4. 작업 활성화
echo 5. 종료
echo.
set /p choice="선택 (1-5): "

if "%choice%"=="1" (
    echo.
    echo 작업 스케줄러를 엽니다...
    taskschd.msc
) else if "%choice%"=="2" (
    echo.
    echo 수동으로 실행합니다...
    schtasks /Run /TN "StockTrader_Auto"
    if %ERRORLEVEL% EQU 0 (
        echo ✅ 실행 성공!
    ) else (
        echo ❌ 실행 실패
    )
) else if "%choice%"=="3" (
    echo.
    echo 작업을 비활성화합니다...
    schtasks /Change /TN "StockTrader_Auto" /DISABLE
    if %ERRORLEVEL% EQU 0 (
        echo ✅ 비활성화 성공!
    ) else (
        echo ❌ 비활성화 실패
    )
) else if "%choice%"=="4" (
    echo.
    echo 작업을 활성화합니다...
    schtasks /Change /TN "StockTrader_Auto" /ENABLE
    if %ERRORLEVEL% EQU 0 (
        echo ✅ 활성화 성공!
    ) else (
        echo ❌ 활성화 실패
    )
)

echo.
pause

