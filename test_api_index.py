import asyncio
from kiwoom_rest import KiwoomRestClient

async def main():
    api = KiwoomRestClient()
    await api._ensure_client()
    await api.check_token_validity()
    
    # Try calling the new method
    print("Fetching kosdaq current...")
    res_curr = await api.get_industry_current_price('101')
    print("current:", res_curr)
    
    print("Fetching kosdaq 3m chart...")
    res_min = await api.get_industry_minute_chart('101', '3')
    print("minute:")
    if res_min.get('inds_min_pole_qry'):
        print(res_min['inds_min_pole_qry'][:2])
    else:
        print(res_min)
        

if __name__ == '__main__':
    asyncio.run(main())

