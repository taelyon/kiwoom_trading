import websockets
from websockets.http11 import Response
from websockets.datastructures import Headers

headers = Headers([
    ("Content-Type", "text/html; charset=utf-8"),
    ("Server", "Antigravity Unified Server")
])
response = Response(status_code=200, reason_phrase="OK", headers=headers, body=b"Hello")
print("Response headers type:", type(response.headers))
try:
    response.headers["Server"] = "Test"
    print("Success, Server header is now:", response.headers["Server"])
except Exception as e:
    print("Error:", e)
