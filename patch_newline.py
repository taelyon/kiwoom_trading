import os

with open('web_dashboard.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace literal newlines inside the confirm dialog with escaped \n
old_str = 'if(confirm("정말 앱을 재시작하시겠습니까?\n\n(프로그램이 강제 종료된 후 NAS Docker에 의해 즉시 자동 재부팅됩니다. 약 10~20초 뒤 화면이 새로고침 됩니다.)")) {'
new_str = 'if(confirm("정말 앱을 재시작하시겠습니까?\\n\\n(프로그램이 강제 종료된 후 NAS Docker에 의해 즉시 자동 재부팅됩니다. 약 10~20초 뒤 화면이 새로고침 됩니다.)")) {'

if old_str in content:
    new_content = content.replace(old_str, new_str)
    with open('web_dashboard.py', 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("Fixed!")
else:
    print("Target string not found.")
