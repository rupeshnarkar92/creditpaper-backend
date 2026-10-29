"""Debug Ollama PDF extraction."""

import base64
import json
import sys
import urllib.request

sys.path.insert(0, "src")
from creditpaper.llm.pdf_render import render_pdf_pages

path = r"D:\OSProjects\cp_docs\deal_files-20260910T041607Z-1-001\deal_files\Deal Documents\Term Sheet\20 Forest Knoll - Updated Executed ITS (Construction).pdf"
content = open(path, "rb").read()
print(f"PDF size: {len(content)} bytes")

pages = render_pdf_pages(content, max_pages=5)
print(f"Rendered {len(pages)} pages")
for i, p in enumerate(pages):
    print(f"  Page {i}: {len(p)} bytes ({len(p) / 1024:.0f} KB)")

# Test a single page with Ollama
b64_png = base64.b64encode(pages[0]).decode("ascii")
payload = {
    "model": "qwen2.5vl:7b",
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_png}"}},
                {"type": "text", "text": "Describe this page in one sentence."},
            ],
        }
    ],
    "stream": False,
}
body = json.dumps(payload).encode("utf-8")
print(f"Request body size: {len(body)} bytes ({len(body) / 1024 / 1024:.1f} MB)")
req = urllib.request.Request(
    "http://localhost:11434/v1/chat/completions",
    data=body,
    method="POST",
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
        result = json.loads(raw)
        content_text = result["choices"][0]["message"]["content"]
        print(f"Response: {content_text[:200]}")
except Exception as e:
    print(f"ERROR: {e}")
    import traceback

    traceback.print_exc()
