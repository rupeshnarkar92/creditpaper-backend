"""Route a document from a three-page probe, and exclude pages from table work.

Route per document, not per page: fixture measurement (spec Sec.2) found 23.4%
of pages have no text layer and whole documents are overwhelmingly one kind or
the other (70 documents wholly image-only), so a 3-page probe (first, middle,
last) answers what per-page decisions would need thousands of reads to answer.

A page with a real text layer is judged on word count alone (``MIN_WORDS``),
never on whether it also carries an image: a vector-only page (drawings, no
raster image, no text) has ``has_image=False`` and no words, and it still needs
OCR like a scanned page - routing it to EMPTY on the presence-of-image test
alone silently skipped OCR for it. EMPTY is reserved for a document with no
pages at all.

The table gate is inverted deliberately: a cheap numeric-alignment detector
scores precision <=0.33 for selecting a table (you would pay to run full table
extraction on ~3 pages for every real table caught) but NPV 98.7% for clearing
one, so it may only exclude a page from expensive table extraction, never
select one.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from creditpaper.ingest.reader import PageContent, PageReader
from creditpaper.ingest.values import numeric_tokens

PROBE_PAGES = 3
MIN_WORDS = 10
_TABLE_GATE_MIN_NUMERIC_LINES = 3
_TABLE_GATE_MIN_TOKENS_PER_LINE = 2


class Route(str, Enum):
    BORN_DIGITAL = "born_digital"
    SCANNED = "scanned"
    MIXED = "mixed"
    EMPTY = "empty"


class ProbeResult(BaseModel):
    route: Route
    probed_pages: list[int]
    text_pages: int
    image_pages: int
    needs_ocr: bool


def _sample_pages(count: int) -> list[int]:
    if count <= PROBE_PAGES:
        return list(range(1, count + 1))
    return sorted({1, (count + 1) // 2, count})


def probe(reader: PageReader) -> ProbeResult:
    count = reader.page_count()
    if count == 0:
        return ProbeResult(route=Route.EMPTY, probed_pages=[], text_pages=0, image_pages=0, needs_ocr=False)

    numbers = _sample_pages(count)
    pages = [reader.page(n) for n in numbers]
    text_pages = sum(1 for p in pages if p.word_count >= MIN_WORDS)
    image_pages = sum(1 for p in pages if p.word_count < MIN_WORDS and p.has_image)

    if text_pages == len(pages):
        route = Route.BORN_DIGITAL
    elif text_pages == 0:
        route = Route.SCANNED
    else:
        route = Route.MIXED

    return ProbeResult(
        route=route,
        probed_pages=numbers,
        text_pages=text_pages,
        image_pages=image_pages,
        needs_ocr=route in (Route.SCANNED, Route.MIXED),
    )


def may_skip_table_extraction(page: PageContent) -> bool:
    """True only when the page confidently has no table (the inverted gate).

    Never the converse: this signal has precision <=0.33 for selecting a page
    as having a table, so it may only clear a page, never select one. A page
    is NOT skippable once 3 or more of its lines each carry 2 or more numeric
    tokens - the minimum form of the gate (spec: column alignment from
    character boxes may tighten it further; not implemented here).
    """
    lines = [line for line in page.text.splitlines() if line.strip()]
    numeric_lines = sum(1 for line in lines if len(numeric_tokens(line)) >= _TABLE_GATE_MIN_TOKENS_PER_LINE)
    return numeric_lines < _TABLE_GATE_MIN_NUMERIC_LINES
