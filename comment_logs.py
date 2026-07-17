import sys

with open('web_dashboard.py', 'r', encoding='utf-8') as f:
    data = f.read()

data = data.replace('logging.debug(f"[WS PROFILE SERVER]', '# logging.debug(f"[WS PROFILE SERVER]')
data = data.replace('logging.debug(f"⚡ [WS PROFILE SERVER]', '# logging.debug(f"⚡ [WS PROFILE SERVER]')

with open('web_dashboard.py', 'w', encoding='utf-8') as f:
    f.write(data)

print("WS PROFILE SERVER logs commented out successfully.")
