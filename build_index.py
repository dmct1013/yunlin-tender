#!/usr/bin/env python3
"""
把 data.json 注入 index_template.html，產生可直接雙擊開啟的 index.html
用法：python3 build_index.py
"""
import json, os

DIR           = os.path.dirname(os.path.abspath(__file__))
data_path     = os.path.join(DIR, 'data.json')
template_path = os.path.join(DIR, 'index_template.html')
output_path   = os.path.join(DIR, 'index.html')

with open(data_path, encoding='utf-8') as f:
    data = json.load(f)

data_js = json.dumps(data, ensure_ascii=False)

with open(template_path, encoding='utf-8') as f:
    html = f.read()

if '__DATA_PLACEHOLDER__' not in html:
    print('錯誤：index_template.html 裡找不到 __DATA_PLACEHOLDER__')
    exit(1)

new_html = html.replace('__DATA_PLACEHOLDER__', data_js)

with open(output_path, 'w', encoding='utf-8') as f:
    f.write(new_html)

print(f'完成！{data["total"]} 筆標案已注入 index.html')
print(f'活動相關：{data["activity_total"]} 筆')
print(f'直接雙擊 index.html 開啟即可')
