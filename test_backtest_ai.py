import asyncio
from backtester import Backtester
import json
import logging

logging.basicConfig(level=logging.INFO)

async def test_backtest():
    b = Backtester()
    b.db_path = 'stock_data.db'
    
    # 전략 정의 (AI_SCORE 사용)
    custom_buy = [{
        "name": "AI 매수 테스트",
        "content": "AI_SCORE > 0.5"
    }]
    custom_sell = [{
        "name": "AI 매도 테스트",
        "content": "AI_SCORE < 0.3"
    }]
    
    # 2026-07-20 특정 종목 테스트 (298020 효성첨단소재)
    result = b.run(
        start_date="2026-07-19", 
        end_date="2026-07-20", 
        code="298020",
        custom_buy=custom_buy,
        custom_sell=custom_sell
    )
    
    print("결과:", json.dumps(result, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(test_backtest())
