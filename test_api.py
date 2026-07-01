
import asyncio
from kiwoom_rest import KiwoomRestClient

async def test():
    client = KiwoomRestClient('.env')
    await client.connect()
    
    # Try 30
    print('Testing 30')
    res30 = await client.get_stock_tic_chart('005930', tic_scope=30)
    print(res30)
    
    # Try 60
    print('Testing 60')
    res60 = await client.get_stock_tic_chart('005930', tic_scope=60)
    print(res60)

if __name__ == '__main__':
    asyncio.run(test())

