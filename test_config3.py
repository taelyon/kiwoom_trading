import os
from config_manager import get_config

with open('.env', 'w') as f:
    f.write("SETTINGS_BUYCOUNT=5\n")
    f.write("SETTINGS_PRIME_CASH=1000000\n")

c = get_config()
c.reload()
print("buycount:", c.get('SETTINGS', 'buycount'))
print("prime_cash:", c.get('SETTINGS', 'prime_cash'))
