import sys

with open('web_dashboard.py', 'r', encoding='utf-8') as f:
    text = f.read()

old_text = 'term.innerText += "\\n✅ [Deploy] 배포 성공: " + data.msg + "\\n";'
new_text = 'term.innerText += "\\n✅ [Deploy] 배포 성공: " + data.msg + "\\n";\n                            fetchModelHistory();'

if old_text in text:
    text = text.replace(old_text, new_text)
    with open('web_dashboard.py', 'w', encoding='utf-8') as f:
        f.write(text)
    print("Success")
else:
    print("Failed to find target text")
