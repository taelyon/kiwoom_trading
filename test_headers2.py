import websockets
from websockets.datastructures import Headers

h = Headers([("Content-Type", "text/html")])
try:
    h["Server"] = "Test"
    print("Set Server successfully")
except Exception as e:
    print(f"Exception type: {type(e)}, message: {e}")
