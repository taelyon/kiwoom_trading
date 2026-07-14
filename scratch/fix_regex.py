data = open('web_dashboard.py', encoding='utf-8').read()

# 실제로 파일에 있는 바이트 시퀀스 확인
idx = data.find("re.split(r'")
if idx >= 0:
    snippet = data[idx:idx+60]
    print(f"Raw snippet: {repr(snippet)}")
    # 직접 교체: r'\\s+and\\s+' -> r'\s+and\s+'
    old = "r'\\\\s+and\\\\s+'"
    new = "r'\\s+and\\s+'"
    if old in data:
        data = data.replace(old, new, 1)
        open('web_dashboard.py', 'w', encoding='utf-8').write(data)
        print("Fixed double backslash!")
    else:
        print(f"Old pattern not found. Trying single...")
        old2 = "r'\\s+and\\s+'"
        if old2 in data:
            print("Already correct (single backslash)")
        else:
            print("Cannot find pattern")
else:
    print("re.split not found at all")
