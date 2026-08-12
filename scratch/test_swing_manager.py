import sys
import os
import asyncio
import pandas as pd
import numpy as np

# Windows Console UTF-8 Encoding Setting
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

# 프로젝트 경로 추가
sys.path.insert(0, r'c:\MyAPP\kiwoom_trading')

from swing_strategy_utils import calc_daily_indicators, prepare_swing_locals, evaluate_swing_condition
from database import AsyncDatabaseManager

async def run_tests():
    print("==========================================")
    print("[스윙 매매 코어 모듈 합성 테스트 시작]")
    print("==========================================")

    # 1. 일봉 가상 데이터 생성 테스트
    dates = pd.date_range(end='2026-08-12', periods=30, freq='B')
    prices = [10000 + (i * 150) + ((-1)**i * 50) for i in range(30)]
    volumes = [100000 + (i * 5000) for i in range(30)]
    
    df_daily = pd.DataFrame({
        'datetime': dates.strftime('%Y%m%d'),
        'open': prices,
        'high': [p + 200 for p in prices],
        'low': [p - 150 for p in prices],
        'close': [p + 50 for p in prices],
        'volume': volumes
    })

    print(f"1. 가상 일봉 샘플 데이터 30일분 생성 완료 (최근 종가: {df_daily['close'].iloc[-1]:,.0f}원)")

    # 2. 일봉 지표 계산 테스트
    df_calc = calc_daily_indicators(df_daily)
    last_row = df_calc.iloc[-1]
    
    print("\n2. 기술적 분석 지표 계산 결과:")
    print(f"  - MA5: {last_row['ma5']:,.1f}")
    print(f"  - MA20: {last_row['ma20']:,.1f}")
    print(f"  - Disparity20: {last_row['disparity20']:.2f}%")
    print(f"  - RSI 14: {last_row['rsi14']:.2f}")
    print(f"  - Volume Ratio: {last_row['volume_ratio']:.2f}")
    print(f"  - MACD Hist: {last_row['macd_hist']:.2f}")

    # 3. safe_locals 및 수식 평가 테스트
    safe_locals = prepare_swing_locals("005930", df_daily)
    rule = "disparity20 >= 98.0 and rsi14 < 70.0 and volume_ratio >= 1.0"
    is_passed = evaluate_swing_condition(rule, safe_locals)
    print(f"\n3. 수식 평가 테스트 ('{rule}'): {'[통과]' if is_passed else '[탈락]'}")

    # 4. DB swing_holdings 비동기 CRUD 테스트
    db = AsyncDatabaseManager()
    await db.init_database()

    test_code = "999999"
    await db.save_swing_holding(test_code, "테스트스윙주식", 15000.0, 100, "2026-08-12", 15500.0, "스윙_종가매수")
    
    holdings = await db.get_swing_holdings()
    print(f"\n4. DB 스윙 보유 종목 저장 및 조회 검증: 총 {len(holdings)}개 등록됨")
    assert test_code in holdings, "DB 저장 검증 실패!"
    print(f"  - 조회 성공: {holdings[test_code]}")

    # 청산 이력 및 삭제 테스트
    await db.save_swing_trade_record(test_code, "테스트스윙주식", 15000.0, 16000.0, 100, 100000.0, 6.67, "2026-08-12", "2026-08-12", "목표가 달성")
    await db.delete_swing_holding(test_code)

    holdings_after = await db.get_swing_holdings()
    assert test_code not in holdings_after, "DB 삭제 검증 실패!"
    print("  - DB 삭제 및 매매 이력 저장 완료!")

    records = await db.get_swing_trade_records(5)
    print(f"  - 매매 이력 기록 수: {len(records)}개")

    print("\n==========================================")
    print("모든 스윙 매매 코어 테스트 100% 성공!")
    print("==========================================")

if __name__ == '__main__':
    asyncio.run(run_tests())
