import asyncio
from backtester import Backtester
import json
import logging

logging.basicConfig(level=logging.INFO)

async def run_batch_test():
    b = Backtester()
    # 주의: 로컬에 'data/stock_data.db' 또는 올바른 db_path 가 설정되어 있어야 합니다.
    # 만약 DB 경로가 다르면 b.db_path = "당신의_DB_경로.db" 로 수정해주세요.
    
    thresholds = [0.55, 0.6, 0.7, 0.8, 0.9]
    results = {}
    
    start_date = "2026-07-20" # 필요시 기간 수정
    end_date = "2026-07-20"   # 필요시 기간 수정
    
    print(f"🚀 AI_SCORE 임계값 배치 백테스트 시작 ({start_date} ~ {end_date})")
    
    for th in thresholds:
        print(f"\n=========================================")
        print(f"📊 테스트 중: AI_SCORE > {th}")
        
        custom_buy = [{
            "name": f"AI 매수 (> {th})",
            "content": f"AI_SCORE > {th}"
        }]
        # 매도 조건은 간단히 이익보전이나 손절라인 등을 추가할 수 있습니다.
        # 여기서는 테스트를 위해 단순 수익률 2% 익절, -2% 손절 조건을 넣습니다.
        custom_sell = [{
            "name": "기본 매도 (수익/손실)",
            "content": "수익률 > 2.0 or 수익률 < -2.0"
        }]
        
        # 'ALL'을 넣으면 전체 종목에 대해 테스트합니다. 시간이 걸릴 수 있습니다.
        # 특정 종목만 테스트하려면 "298020" 처럼 종목 코드를 넣어주세요.
        result = b.run(
            start_date=start_date, 
            end_date=end_date, 
            code="ALL", 
            custom_buy=custom_buy,
            custom_sell=custom_sell,
            initial_capital=10000000,
            buycount=3
        )
        
        # 결과 요약 저장
        if "error" not in result:
            results[th] = {
                "총 수익": result.get("total_profit", 0),
                "승률": result.get("win_rate", 0),
                "매매 횟수": result.get("total_trades", 0),
                "최종 자본": result.get("final_capital", 0)
            }
            print(f"✅ 완료: 총 수익 {results[th]['총 수익']:,.0f}원 | 승률 {results[th]['승률']:.2f}% | 매매 횟수 {results[th]['매매 횟수']}회")
        else:
            print(f"❌ 에러 발생: {result['error']}")
            results[th] = "Error"
            
    print("\n=========================================")
    print("🏆 최종 배치 테스트 결과 요약")
    for th, res in results.items():
        if isinstance(res, dict):
            print(f"[AI_SCORE > {th}] 총 수익: {res['총 수익']:,.0f}원 | 승률: {res['승률']:.2f}% | 매매 횟수: {res['매매 횟수']}회")
        else:
            print(f"[AI_SCORE > {th}] {res}")

if __name__ == "__main__":
    asyncio.run(run_batch_test())
