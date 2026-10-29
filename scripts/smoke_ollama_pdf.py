"""Operator-driven smoke for the Ollama PDF path.

Not a test. Run with:
    python scripts/smoke_ollama_pdf.py

Prints what the messages_create call returns when handed a real PDF.

KNOWN LIMITATION (2026-08-30): the Ollama adapter renders every PDF page
(30-page cap) and sends all PNGs as image content blocks. For a 16-page
credit paper that exceeds Ollama's per-request body limit and the call
returns HTTP 400. For PDFs of 1-3 pages the path works end-to-end
(verified with qwen2.5-vl:7b). Follow-up: cap the Ollama path at 3 pages
or pre-shrink the page count to match the model's actual context budget.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["LLM_PROVIDER"] = "ollama"
os.environ["OLLAMA_MODEL"] = "qwen2.5vl:7b"
os.environ["OLLAMA_BASE_URL"] = "http://localhost:11434/v1"

sys.path.insert(0, "src")

from creditpaper.llm.client import messages_create

PDF_PATH = Path(
    r"D:\OSProjects\creditpaper\output\tenants\dev\onboarding\papers\04875ea1-5bad-4a08-aa16-b1882c7e31a4\Anonymised_Internal_Credit_Paper.pdf"
)

if not PDF_PATH.exists():
    print(f"PDF not found at {PDF_PATH}")
    raise SystemExit(1)

print(f"Reading PDF from {PDF_PATH} ({PDF_PATH.stat().st_size} bytes)")
content = PDF_PATH.read_bytes()

print("Calling messages_create with the PDF as a base64 Anthropic document block...")
import base64  # noqa: E402
import time  # noqa: E402

t0 = time.monotonic()
resp = messages_create(
    tool="smoke:ollama_pdf",
    model="qwen2.5vl:7b",
    max_tokens=200,
    system="You are a credit-document analyst. Look at the pages and report the first section heading and the first 3 numeric values you see.",
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "What is the first section heading and three numbers in this document?",
                },
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(content).decode("ascii"),
                    },
                },
            ],
        }
    ],
)
dt = time.monotonic() - t0

print(f"\nResponse in {dt:.1f}s:")
print(f"  text: {resp.text[:500]!r}")
print(f"  stop_reason: {resp.stop_reason}")
print(f"  input_tokens: {resp.input_tokens}")
print(f"  output_tokens: {resp.output_tokens}")
