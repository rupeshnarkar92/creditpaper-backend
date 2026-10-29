"""Operator smoke: run the brand analyzer through Ollama end-to-end."""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("LLM_PROVIDER", "ollama")
os.environ.setdefault("OLLAMA_MODEL", "qwen2.5vl:7b")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434/v1")

sys.path.insert(0, "src")

import httpx

base = "http://localhost:8000"
headers = {"X-Tenant-ID": "1"}

# Use a small public page to keep the HTTP fetch fast.
url = "https://example.com"
print(f"Analyzing {url} via Ollama...")
t0 = time.monotonic()
r = httpx.post(
    f"{base}/onboarding/brand/analyze",
    json={"website_url": url},
    headers=headers,
    timeout=180,
)
dt = time.monotonic() - t0
print(f"\nBrand analyze in {dt:.2f}s — status {r.status_code}")
if r.status_code == 200:
    data = r.json()
    print(f"  status: {data.get('status')}")
    print(f"  name: {data.get('brand', {}).get('name')}")
    print(f"  primary: {data.get('brand', {}).get('colors', {}).get('primary')}")
    print(f"  vocabulary (first 5): {(data.get('brand', {}).get('vocabulary') or [])[:5]}")
else:
    print(f"  body: {r.text[:500]}")
