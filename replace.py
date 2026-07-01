
import os

files_to_update = ['web_dashboard.py', 'strategy.py']
for file in files_to_update:
    with open(file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    content = content.replace('매수 전략', '매수 로직')
    content = content.replace('매도 전략', '매도 로직')
    
    with open(file, 'w', encoding='utf-8') as f:
        f.write(content)
print('Replacement complete.')

