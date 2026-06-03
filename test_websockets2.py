import asyncio
import websockets
from websockets.server import serve

def process_request(arg1, arg2):
    print("arg1:", type(arg1))
    print("arg2:", type(arg2))
    
    if hasattr(arg2, 'headers'):
        print("Upgrade header:", arg2.headers.get("Upgrade"))
        print("Path:", arg2.path)
    else:
        print("Legacy mode")
    
    return None

async def main():
    async with serve(lambda ws: None, "localhost", 8085, process_request=process_request):
        pass

asyncio.run(main())
