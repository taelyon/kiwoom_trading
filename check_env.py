import os
env_path = '.env'
if os.path.exists(env_path):
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            if 'prime_cash' in line.lower():
                print(line.strip())
else:
    print(".env not found")
