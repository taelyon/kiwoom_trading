import asyncio
from http import HTTPStatus
from websockets.http11 import Response
import websockets

def health_check(connection, request):
    print("Received request:", request.path)
    
    html_content = b"<html><body>Hello</body></html>"
    # Create an HTTP Response object manually
    headers = [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(html_content)))]
    response = Response(status_code=200, reason_phrase="OK", headers=headers, body=html_content)
    return response

print("websockets version:", websockets.__version__)
