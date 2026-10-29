"""Vision for machine-read pages, with Tesseract as the cross-check (spec §3.2).

A page with no text layer is read by Tesseract at parse time
(`extractors/pdf.py`). OCR is the weakest text the pipeline holds: measured
2026-09-25, facts read off OCR'd pages are markedly less stable, and 42% of
support checks on them come back "unclear". So where the call would reach
Anthropic, each such page is ALSO transcribed by vision
(`ingest/transcribe.fill_text_less`, cached per document content hash, so a
document is paid for once):

- the vision text replaces the OCR text (it reads tables and small print better);
- the two reads are compared. When they agree — closely in wording and EXACTLY
  in every figure — two independent reads stand behind the page, and it is no
  longer treated as machine-read: the review cap on its facts is lifted;
- when they do not, or Tesseract read nothing, the page stays machine-read and
  its facts stay capped for an analyst.

A vision failure leaves the OCR text exactly as it was. Born-digital pages are
never sent. Other providers keep Tesseract alone.

`machine_read(page_texts)` is the one reading of these flags every consumer uses.
"""

from __future__ import annotations

import base64
import io
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from rapidfuzz import fuzz

from creditpaper.ingest.parse_cache import content_hash
from creditpaper.ingest.transcribe import VISION_SOURCE, fill_text_less, is_text_less
from creditpaper.ingest.values import numeric_tokens
from creditpaper.llm.providers import LLMProvider
from creditpaper.processing.models import ExtractionResult

log = logging.getLogger(__name__)

__all__ = ["AGREEMENT", "enrich", "machine_read", "reads_agree"]

# Wording similarity two reads of one page need to count as agreeing. Figures
# must match exactly on top of it: a single misread digit is the failure that
# matters, and a similarity score would forgive it.
AGREEMENT = 0.90
_RENDER_DPI = 150


def reads_agree(ocr_text: str, vision_text: str) -> bool:
    """Two independent reads of one page agree: close in wording, identical in figures."""
    a, b = " ".join(ocr_text.split()), " ".join(vision_text.split())
    if not a or not b:
        return False
    if sorted(numeric_tokens(a)) != sorted(numeric_tokens(b)):
        return False
    return fuzz.ratio(a.casefold(), b.casefold()) / 100 >= AGREEMENT


def machine_read(page_texts: Sequence[Mapping[str, Any]]) -> frozenset[int]:
    """Page numbers whose text is a machine reading no second read confirmed: their facts are capped."""
    return frozenset(
        int(p["page"])
        for p in page_texts
        if (p.get("ocr_used") or p.get("ocr_failed") or p.get("ocr_deferred"))
        and not p.get("ocr_agrees")
    )


def _provider(tenant_id: str) -> LLMProvider:
    from creditpaper.llm.client import effective_provider
    from creditpaper.llm.tenant_config import provider_session

    with provider_session(tenant_id):
        return effective_provider()


def _render(raw: bytes) -> Callable[[list[int]], list[str]]:
    from creditpaper.ingest.reader import render_pages

    def render(indices: list[int]) -> list[str]:
        out: list[str] = []
        for image in render_pages(raw, indices, dpi=_RENDER_DPI):
            if image is None:
                raise RuntimeError("a machine-read page could not be rendered")
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            out.append(base64.standard_b64encode(buf.getvalue()).decode("ascii"))
        return out

    return render


def enrich(tenant_id: str, raw: bytes, result: ExtractionResult) -> ExtractionResult:
    """`result` with machine-read pages transcribed by vision and cross-checked; unchanged when none apply."""
    pages = [dict(p) for p in result.page_texts]
    machine = [
        p
        for p in pages
        if p.get("ocr_used")
        or p.get("ocr_failed")
        or p.get("ocr_deferred")
        or is_text_less(str(p.get("text") or ""))
    ]
    if not machine:
        return result
    if _provider(tenant_id) != LLMProvider.ANTHROPIC:
        return result
    machine_numbers = {int(p["page"]) for p in machine}
    # The text LAYER, as vision sees it: a machine-read page is offered as text-less.
    layer = [
        {"page": p["page"], "text": "" if int(p["page"]) in machine_numbers else p.get("text", "")}
        for p in pages
    ]
    try:
        from creditpaper.llm.tenant_config import provider_session

        with provider_session(tenant_id):
            filled = fill_text_less(tenant_id, content_hash(raw), layer, _render(raw))
    except Exception as exc:  # the OCR text stands
        log.warning("vision transcription failed: %s", type(exc).__name__)
        return result
    by_page = {int(p["page"]): p for p in filled}
    for page in pages:
        n = int(page["page"])
        read = by_page.get(n, {})
        if n not in machine_numbers or read.get("source") != VISION_SOURCE:
            continue
        ocr_text = str(page.get("text") or "")
        page["ocr_used"] = True
        page["ocr_agrees"] = reads_agree(ocr_text, str(read["text"]))
        page["text"] = str(read["text"])
        page["source"] = VISION_SOURCE
    return result.model_copy(
        update={"page_texts": pages, "full_text": "\n\n".join(str(p.get("text") or "") for p in pages)}
    )
