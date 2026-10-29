"""Debug what the extractor actually sends to Ollama."""

import json
import sys

sys.path.insert(0, "src")

from creditpaper.extractors._doc import b64
from creditpaper.llm.client import _anthropic_kwargs_to_ollama_messages

path = r"D:\OSProjects\cp_docs\deal_files-20260910T041607Z-1-001\deal_files\Deal Documents\Term Sheet\20 Forest Knoll - Updated Executed ITS (Construction).pdf"
pdf_b64 = b64(path)
print(f"PDF base64 length: {len(pdf_b64)}")

# Simulate what document_json sends
instruction = "Extract facility amount and interest rate. Return JSON only."
doc_block = {
    "type": "document",
    "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64},
}

kwargs = {
    "model": "qwen2.5vl:7b",
    "max_tokens": 8000,
    "messages": [{"role": "user", "content": [doc_block, {"type": "text", "text": instruction}]}],
}

# Convert to Ollama format
messages = _anthropic_kwargs_to_ollama_messages(kwargs)
print(f"\nConverted messages: {len(messages)} messages")

# Inspect structure
for i, msg in enumerate(messages):
    role = msg.get("role")
    content = msg.get("content")
    if isinstance(content, list):
        print(f"\nMessage {i} ({role}): {len(content)} content blocks")
        for j, block in enumerate(content):
            btype = block.get("type")
            if btype == "text":
                print(f"  Block {j}: text ({len(block.get('text', ''))} chars)")
            elif btype == "image_url":
                url = block.get("image_url", {}).get("url", "")
                print(f"  Block {j}: image_url (url length: {len(url)} chars)")
                # Check format
                if url.startswith("data:image/png;base64,"):
                    print("    -> starts with data:image/png;base64,")
                else:
                    print(f"    -> starts with: {url[:50]}...")
            else:
                print(f"  Block {j}: {btype}")
    elif isinstance(content, str):
        print(f"\nMessage {i} ({role}): string ({len(content)} chars)")

# Now test sending this to Ollama
import urllib.request  # noqa: E402

payload = {"model": "qwen2.5vl:7b", "messages": messages, "stream": False}
body = json.dumps(payload).encode("utf-8")
print(f"\nFinal request body size: {len(body)} bytes ({len(body) / 1024 / 1024:.1f} MB)")

# Count image blocks
img_count = sum(
    1
    for m in messages
    if isinstance(m.get("content"), list)
    for b in m["content"]
    if b.get("type") == "image_url"
)
print(f"Image blocks: {img_count}")

print("\nSending to Ollama...")
req = urllib.request.Request(
    "http://localhost:11434/v1/chat/completions",
    data=body,
    method="POST",
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = resp.read()
        result = json.loads(raw)
        content_text = result["choices"][0]["message"]["content"]
        print(f"Response ({len(content_text)} chars): {content_text[:300]}...")
except urllib.error.HTTPError as e:
    print(f"HTTP Error {e.code}: {e.reason}")
    body_text = e.read().decode("utf-8", errors="replace")
    print(f"Response body: {body_text[:500]}")
except Exception as e:
    print(f"ERROR: {e}")
    import traceback

    traceback.print_exc()
