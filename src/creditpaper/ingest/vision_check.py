"""Targeted vision: a second, independent read of only the scanned pages that matter.

Vision on every scanned page (`processing/vision.py`, at parse time) measured
~$0.018 a page: ~$2.90 on one deal's slotted documents, more than the whole
ingestion target, mostly spent on plans, appendices and signature pages no fact
stands on (Jason, 2026-10-09: "make vision targeted").

What vision is FOR is the fact read off OCR: OCR is the weakest text the pipeline
holds, so such a fact is capped below the review threshold (`verify.Reason.OCR_TEXT`).
This runs after extraction and reads only the scanned pages a WANTED fact (material,
or an item of a list) stands on, material pages first, within a page budget:

- vision's read states the same value: two independent reads agree, so the cap is
  lifted (`CONFIRMED_CONFIDENCE`, reason `vision_confirmed`);
- it does not: the fact stays capped, marked `vision_disagrees`, for an analyst;
- the read fails: the fact is left exactly as it was.

Every other scanned page is never sent.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from creditpaper.model.apply import ExtractionOutput
from creditpaper.model.core import Fact, VerificationSummary

log = logging.getLogger(__name__)

__all__ = [
    "CONFIRMED_CONFIDENCE",
    "Tier",
    "VisionCheckReport",
    "confirm_in_tiers",
    "confirm_on_machine_read",
    "read_page_by_second_ocr",
    "read_page_by_vision",
]

# Two independent reads (OCR and vision) state the value: above the review threshold,
# below the exact-anchor confidence a born-digital page earns.
CONFIRMED_CONFIDENCE = 0.90
MAX_PAGES = 40
_MACHINE_REASONS = ("ocr_text", "vision_text")


@dataclass
class VisionCheckReport:
    candidates: int = 0  # wanted facts standing on a machine-read page
    pages_read: int = 0
    confirmed: int = 0
    disputed: int = 0
    failed: int = 0  # pages vision could not read
    skipped_over_budget: int = 0  # wanted pages beyond the page budget


def _capped(fact: Fact[Any]) -> bool:
    summary = fact.verification
    return summary is not None and any(r in _MACHINE_REASONS for r in summary.reasons)


def _states(text: str, value: object) -> bool:
    """Whether the page text states the value: the same figure, or the same words."""
    from creditpaper.ingest.consensus import _key
    from creditpaper.ingest.values import figures

    wanted = _key(value)
    if any(_key(f.value) == wanted for f in figures(text)):
        return True
    words = " ".join(str(value).casefold().split())
    return (
        bool(words) and not any(ch.isdigit() for ch in words) and words in " ".join(text.casefold().split())
    )


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", text.casefold()))


def _states_beside(text: str, value: object, quote: str) -> bool:
    """Whether the read states the value on a line next to the words it was quoted with.

    A line and its neighbours, because an OCR engine often returns a table's label and
    its figure as separate boxes on consecutive lines. A quote with no words (a bare
    figure in a table cell) has nothing to check against, so any line that states the
    value counts."""
    context = _words(quote) - _words(str(value))
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not _states(line, value):
            continue
        if not context or _words(" ".join(lines[max(0, i - 1) : i + 2])) & context:
            return True
    return False


def _with(fact: Fact[Any], *, confidence: float | None, reason: str) -> Fact[Any]:
    summary = fact.verification
    assert summary is not None
    reasons = tuple(r for r in summary.reasons if r not in _MACHINE_REASONS or confidence is None) + (reason,)
    new_summary = VerificationSummary(
        verdict=summary.verdict,
        reasons=reasons,
        confidence=confidence if confidence is not None else summary.confidence,
    )
    return fact.model_copy(
        update={
            "verification": new_summary,
            "confidence": confidence if confidence is not None else fact.confidence,
        }
    )


def confirm_on_machine_read(
    outputs: Sequence[ExtractionOutput],
    files_by_document: Mapping[str, Path],
    *,
    read_page: Callable[[Path, int], str],
    wanted: Callable[[str], bool],
    max_pages: int = MAX_PAGES,
    workers: int = 8,
    confirmed_reason: str = "vision_confirmed",
    disputed_reason: str = "vision_disagrees",
    same_line: bool = False,
) -> VisionCheckReport:
    """Read the scanned pages wanted facts stand on, and settle each fact; `outputs` are updated in place."""
    from creditpaper.concurrency import pool
    from creditpaper.model.concepts import concept_for

    report = VisionCheckReport()
    on_page: dict[tuple[str, int], list[tuple[ExtractionOutput, str]]] = {}
    material_pages: set[tuple[str, int]] = set()
    for output in outputs:
        for path, fact in output.fields.items():
            prov = fact.provenance
            if prov is None or prov.page is None or not _capped(fact) or not wanted(path):
                continue
            if prov.document_id not in files_by_document:
                continue
            page = (prov.document_id, prov.page)
            on_page.setdefault(page, []).append((output, path))
            report.candidates += 1
            concept = concept_for(path)
            if concept is not None and concept.material:
                material_pages.add(page)
    ordered = sorted(on_page, key=lambda p: (p not in material_pages, p))
    chosen, report.skipped_over_budget = ordered[:max_pages], max(0, len(ordered) - max_pages)
    if not chosen:
        return report

    def read(page: tuple[str, int]) -> str | None:
        try:
            return read_page(files_by_document[page[0]], page[1])
        except Exception as exc:  # the fact stays as it was
            log.warning("vision check could not read a page: %s", type(exc).__name__)
            return None

    with pool(min(workers, len(chosen)), thread_name_prefix="vision-check") as executor:
        texts = dict(zip(chosen, executor.map(read, chosen), strict=True))
    for page, text in texts.items():
        if text is None:
            report.failed += 1
            continue
        report.pages_read += 1
        for output, path in on_page[page]:
            fact = output.fields[path]
            quote = fact.provenance.location if fact.provenance is not None else ""
            if _states_beside(text, fact.value, quote) if same_line else _states(text, fact.value):
                output.fields[path] = _with(fact, confidence=CONFIRMED_CONFIDENCE, reason=confirmed_reason)
                report.confirmed += 1
            else:
                output.fields[path] = _with(fact, confidence=None, reason=disputed_reason)
                report.disputed += 1
    return report


@dataclass(frozen=True)
class Tier:
    """One reader in the cascade. `read_page` None means the reader is not available here."""

    name: str  # names the reasons: "<name>_confirmed" / "<name>_disagrees"
    read_page: Callable[[Path, int], str] | None
    wanted: Callable[[str], bool]
    max_pages: int = MAX_PAGES
    workers: int = 8
    # Confirm only beside the quote's words (`_states_beside`). For a reader whose page
    # text keeps lines (OCR); a vision transcription re-flows the page, so it does not.
    same_line: bool = False


def confirm_in_tiers(
    outputs: Sequence[ExtractionOutput],
    files_by_document: Mapping[str, Path],
    tiers: Sequence[Tier],
) -> list[VisionCheckReport]:
    """Cheapest reader first; each later tier sees only the facts still capped.

    A fact the free local read confirms never reaches a paid one. A fact it disputes
    stays capped (with the dispute on its trail) and goes to the next tier, so the
    paid read is spent only where two cheaper reads disagreed or could not be had."""
    reports: list[VisionCheckReport] = []
    for tier in tiers:
        if tier.read_page is None:
            reports.append(VisionCheckReport())
            continue
        reports.append(
            confirm_on_machine_read(
                outputs,
                files_by_document,
                read_page=tier.read_page,
                wanted=tier.wanted,
                max_pages=tier.max_pages,
                workers=tier.workers,
                same_line=tier.same_line,
                confirmed_reason=f"{tier.name}_confirmed",
                disputed_reason=f"{tier.name}_disagrees",
            )
        )
    return reports


def read_page_by_second_ocr() -> Callable[[Path, int], str] | None:
    """A `read_page` through the second local OCR engine, or None where there is no
    independent second engine: it is not installed, or Tesseract is missing and the
    parse already read the scans with it (`processing/ocr.ocr_page`)."""
    from creditpaper.processing import ocr, ocr_rapid

    if not ocr_rapid.available() or ocr.tesseract_path() is None:
        return None
    from creditpaper.ingest.reader import render_pages

    def read(path: Path, page: int) -> str:
        (image,) = render_pages(path, [page - 1], dpi=SECOND_OCR_DPI)
        if image is None:
            raise RuntimeError("page could not be rendered")
        return ocr_rapid.read_image(image)

    return read


# The parse reads scans at 200 dpi (`processing/extractors/pdf.py`); the second read
# renders the same page at the same resolution, so the engines differ, not the input.
SECOND_OCR_DPI = 200


def read_page_by_vision(*, model: str | None = None) -> Callable[[Path, int], str]:
    """A `read_page` that renders one page and transcribes it (`ingest/transcribe.py`)."""
    import base64
    import io

    from creditpaper.ingest.reader import render_pages
    from creditpaper.ingest.transcribe import transcribe_page

    def read(path: Path, page: int) -> str:
        (image,) = render_pages(path, [page - 1], dpi=150)
        if image is None:
            raise RuntimeError("page could not be rendered")
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return transcribe_page(base64.standard_b64encode(buf.getvalue()).decode("ascii"), model=model)

    return read
