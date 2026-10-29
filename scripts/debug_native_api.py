"""Direct test of native /api/chat endpoint."""

import base64
import json
import sys
import urllib.request

sys.path.insert(0, "src")
from creditpaper.llm.pdf_render import render_pdf_pages

path = r"D:\OSProjects\cp_docs\deal_files-20260910T041607Z-1-001\deal_files\Deal Documents\Term Sheet\20 Forest Knoll - Updated Executed ITS (Construction).pdf"
content = open(path, "rb").read()

pages = render_pdf_pages(content, max_pages=3)
print(f"Pages: {len(pages)}")

messages = [{"role": "user", "content": []}]
for p in pages:
    b64_png = base64.b64encode(p).decode("ascii")
    messages[0]["content"].append(
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_png}"}}
    )
messages[0]["content"].append({"type": "text", "text": "Extract facility amount. JSON only."})

payload = {
    "model": "qwen2.5vl:7b",
    "messages": messages,
    "stream": False,
    "options": {"num_ctx": 16384},
}

body = json.dumps(payload).encode("utf-8")
print(f"Body: {len(body)} bytes")

req = urllib.request.Request(
    "http://localhost:11434/api/chat", data=body, method="POST", headers={"Content-Type": "application/json"}
)
try:
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = resp.read()
        result = json.loads(raw)
        print(f"Response: {json.dumps(result, indent=2)[:500]}")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.reason}")
    print(f"Body: {e.read().decode()[:500]}")
except Exception as e:
    print(f"Error: {e}")
