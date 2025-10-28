@echo off
chcp 65001 >nul 2>&1
setlocal

cd /d "%~dp0"

echo.
echo ==========================================
echo  Stock Trader 자동 실행 설정
echo ==========================================
echo.
echo 월~금요일 오전 8:40에 자동 실행됩니다.
echo.

REM 현재 디렉토리의 절대 경로 확인
set "CURRENT_DIR=%CD%"
set "EXE_PATH=%CURRENT_DIR%\dist\stock_trader.exe"

REM 실행 파일 존재 확인
if not exist "%EXE_PATH%" (
    echo [오류] stock_trader.exe 파일을 찾을 수 없습니다.
    echo 경로: %EXE_PATH%
    echo.
    echo 먼저 빌드.bat를 실행하여 exe 파일을 생성하세요.
    pause
    exit /b 1
)

echo 실행 파일 확인: %EXE_PATH%
echo.

REM 기존 작업 삭제 (있는 경우)
echo 기존 작업 확인 중...
schtasks /Query /TN "StockTrader_Auto" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo 기존 작업이 있습니다. 삭제 후 재등록합니다.
    schtasks /Delete /TN "StockTrader_Auto" /F >nul 2>&1
)

echo.
echo 작업 스케줄러 등록 중...
echo.

REM 작업 스케줄러 생성
schtasks /Create /TN "StockTrader_Auto" ^
    /TR "\"%EXE_PATH%\"" ^
    /SC WEEKLY ^
    /D MON,TUE,WED,THU,FRI ^
    /ST 08:40 ^
    /RL HIGHEST ^
    /F

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ==========================================
    echo  등록 성공!
    echo ==========================================
    echo.
    echo ✅ 작업 이름: StockTrader_Auto
    echo ✅ 실행 시간: 월~금 오전 8:40
    echo ✅ 실행 파일: %EXE_PATH%
    echo ✅ 권한: 최고 권한으로 실행
    echo.
    echo 작업 스케줄러에서 확인하려면:
    echo - Win + R → taskschd.msc → Enter
    echo - "작업 스케줄러 라이브러리"에서 "StockTrader_Auto" 찾기
    echo.
    echo 수동 테스트:
    echo   schtasks /Run /TN "StockTrader_Auto"
    echo.
) else (
    echo.
    echo ==========================================
    echo  등록 실패
    echo ==========================================
    echo.
    echo [오류] 작업 스케줄러 등록에 실패했습니다.
    echo.
    echo 해결 방법:
    echo 1. 관리자 권한으로 실행했는지 확인
    echo 2. 작업 스케줄러 서비스가 실행 중인지 확인
    echo 3. 수동으로 등록: taskschd.msc 실행 후 수동 설정
    echo.
)

pause

