"""Operator smoke: run the voice generation through Ollama end-to-end.

This is the full pipeline that would run during onboarding Step 3:
1. Load the brand profile (or trigger a re-scrape).
2. Load the validated papers from the DB.
3. Concatenate their PyMuPDF-extracted text.
4. POST the corpus to the LLM and ask for a voice profile.
5. Save the voice profile to the DB.

Currently the tenant "1" has no papers uploaded. We need at least one
validated paper to trigger voice generation. So this smoke:
- Uploads a paper via the API (PDF file).
- Generates the voice.
- Reports the result.

Run with:
    set LLM_PROVIDER=ollama
    set OLLAMA_MODEL=qwen2.5vl:7b
    python scripts/smoke_full_voice_flow.py
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("LLM_PROVIDER", "ollama")
os.environ.setdefault("OLLAMA_MODEL", "qwen2.5vl:7b")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434/v1")

sys.path.insert(0, "src")


# Build a small synthetic "credit paper" PDF in memory — 2 pages, 1 figure each.
# This exercises the LLM path without depending on a tenant PDF.
def make_synthetic_pdf() -> bytes:
    sys.path.insert(0, ".")
    from tests.pdf_builder import Text, build_pdf

    return build_pdf(
        *[
            [Text(72, 72, body)]
            for body in [
                "INTERNAL CREDIT PAPER\nAcme Capital — 14 Marlowe Crescent, Coogee NSW\n"
                "First Mortgage Residential Construction Facility\n\n"
                "Loan amount: $7,500,000\nSponsor equity: $7,000,000\nLVR: 45% against 45% ceiling.\n"
                "Approved by Credit Committee on 2026-06-15.\n\n"
                "The facility binds against the leverage covenant at funding with negligible headroom.",
                "SECURITY\nThe facility is secured by a first ranking mortgage over the subject site. "
                "General security agreement, assignment of contracts and insurances, and personal "
                "guarantees from the sponsors. Valuation by CBRE dated 2026-05-20, as-if-complete $16.7M.\n\n"
                "Risk mitigants: pre-sales at 80% of net sellable area, fixed-price construction contract, "
                "and parental guarantee from Acme Holdings Pty Ltd.",
            ]
        ]
    )


def main() -> None:
    import httpx

    base = "http://localhost:8000"
    headers = {"X-Tenant-ID": "1"}
    pdf_bytes = make_synthetic_pdf()
    print(f"Built synthetic PDF: {len(pdf_bytes)} bytes")

    # Step 1: upload via the API.
    files = {"file": ("smoke.pdf", pdf_bytes, "application/pdf")}
    t0 = time.monotonic()
    r = httpx.post(f"{base}/onboarding/papers", files=files, headers=headers, timeout=60)
    dt = time.monotonic() - t0
    print(f"\nUpload in {dt:.2f}s — status {r.status_code}")
    if r.status_code != 200:
        print(f"  body: {r.text[:500]}")
        return
    doc_info = r.json()["document"]
    print(f"  id: {doc_info['id']}")
    print(f"  status: {doc_info['status']}")
    print(f"  is_credit_paper: {doc_info['is_credit_paper']}")
    print(f"  approval_detected: {doc_info['approval_detected']}")
    print(f"  confidence: {doc_info['confidence']}")
    print(f"  tags: {doc_info.get('tags', [])}")

    # Step 2: confirm validated count.
    r = httpx.get(f"{base}/onboarding/papers", headers=headers, timeout=10)
    data = r.json()
    print(f"\n  validated_count: {data['validated_count']} / {data['required']}")
    print(f"  can_continue: {data['can_continue']}")

    # Step 3: voice status.
    r = httpx.get(f"{base}/onboarding/voice/status", headers=headers, timeout=10)
    print(f"\n  voice can_generate: {r.json().get('can_generate')}")

    # Step 4: generate voice via Ollama.
    t0 = time.monotonic()
    r = httpx.post(f"{base}/onboarding/voice/generate", json={}, headers=headers, timeout=600)
    dt = time.monotonic() - t0
    print(f"\nVoice generation in {dt:.2f}s — status {r.status_code}")
    if r.status_code == 200:
        v = r.json()
        print(f"  tone: {v.get('style', {}).get('tone')}")
        print(f"  register: {v.get('style', {}).get('register')}")
        print(f"  structure (first 3): {v.get('structure', [])[:3]}")
        print(f"  terminology (first 5): {v.get('terminology', [])[:5]}")
        print(f"  source_papers_count: {v.get('source_papers_count')}")
        print(f"  tenant_voice (first 300): {(v.get('samples', {}).get('tenant_voice') or '')[:300]!r}")
    else:
        print(f"  body: {r.text[:500]}")


if __name__ == "__main__":
    main()
