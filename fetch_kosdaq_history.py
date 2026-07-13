import asyncio
import logging
import sys
from kiwoom_rest import KiwoomRestClient
from database import AsyncDatabaseManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

async def main():
    api = KiwoomRestClient()
    await api._ensure_client()
    if not await api.check_token_validity():
        logging.error("Failed to get API token.")
        sys.exit(1)
        
    db = AsyncDatabaseManager()
    await db.init_database()
    
    next_key = ''
    cont_yn = 'N'
    
    for i in range(20): # Fetch up to 20 pages
        logging.info(f"Fetching KOSDAQ 3m chart (Page {i+1})...")
        res = await api.get_industry_minute_chart('101', '3', cont_yn=cont_yn, next_key=next_key)
        
        if not res or 'inds_min_pole_qry' not in res:
            logging.error(f"Failed to fetch or no data. Response: {res}")
            break
            
        data_list = res['inds_min_pole_qry']
        if not data_list:
            break
            
        await db.save_kosdaq_data(data_list)
        
        cont_yn = res.get('cont-yn', 'N')
        next_key = res.get('next-key', '')
        
        if cont_yn != 'Y' or not next_key:
            logging.info("No more pages available.")
            break
            
        await asyncio.sleep(1.5) # Rate limit safety
        
    logging.info("Finished fetching KOSDAQ history.")

if __name__ == '__main__':
    asyncio.run(main())
