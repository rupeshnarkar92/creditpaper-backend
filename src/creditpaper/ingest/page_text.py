"""The one way ingestion reads a document's text.

An upload already goes through `processing.pipeline.process_version`, which
extracts per page with pypdfium2 and falls back to Tesseract on any page with no
text layer, then writes the result to `deal_document_extractions.page_texts`.
That row is authoritative: the workspace reads it, provenance anchors are
verified against it, and roughly a quarter of real documents only have text at
all because of the OCR fallback on that path.

Ingestion therefore reads it rather than parsing again. A second parser would
mean a fact anchored to text the rest of the product cannot see.

The route comes from the `ocr_used` flags the extractor already recorded, which
is the same question Plan A's three-page probe asked of the file - answered from
work that has already been done.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from creditpaper.db import tenant_cursor
from creditpaper.ingest.probe import Route

# Identity of the parse path, for cache keying. Bump it whenever the extractor
# or the OCR step changes in a way that would produce different text, so cached
# parses from the old path are not served for the new one.
PARSER_VERSION = (
    "pdfium+tesseract/3"  # selective OCR + explicit OCR failure provenance
)


class VersionPages(BaseModel):
    version_id: str
    pages: dict[int, str]
    page_count: int
    route: Route
    ocr_pages: int
    # Pages whose text is a machine reading no second read confirmed
    # (`processing/vision.machine_read`): facts anchored there are capped.
    machine_read: frozenset[int] = frozenset()


def _route(total: int, ocr_pages: int) -> Route:
    if total == 0:
        return Route.EMPTY
    if ocr_pages == 0:
        return Route.BORN_DIGITAL
    if ocr_pages == total:
        return Route.SCANNED
    return Route.MIXED


def machine_read(page_texts: list[Any]) -> frozenset[int]:
    from creditpaper.processing.vision import machine_read as _machine_read

    return _machine_read([p for p in page_texts if isinstance(p, dict)])


def pages_for_version(tenant_id: str, version_id: str) -> VersionPages | None:
    """Page text for one document version, or None when it has not been parsed.

    None is not an empty document: it means the pipeline has not run or failed,
    and the caller must not treat that as "this document says nothing".
    """
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT page_texts, page_count FROM deal_document_extractions "
            "WHERE tenant_id = %s AND version_id = %s",
            (tenant_id, version_id),
        )
        row = cur.fetchone()
    if not row:
        return None

    raw: Any = row[0] if isinstance(row[0], list) else json.loads(row[0] or "[]")
    pages: dict[int, str] = {}
    ocr_pages = 0
    for entry in raw:
        number = int(entry.get("page", 0) or 0)
        if number <= 0:
            continue
        pages[number] = str(entry.get("text") or "")
        if entry.get("ocr_used"):
            ocr_pages += 1

    return VersionPages(
        version_id=version_id,
        pages=pages,
        page_count=int(row[1] or len(pages)),
        route=_route(len(pages), ocr_pages),
        ocr_pages=ocr_pages,
        machine_read=machine_read(raw),
    )


def first_pages(pages: dict[int, str], count: int = 3) -> dict[int, str]:
    """The opening pages, which is all classification needs."""
    return {n: pages[n] for n in sorted(pages)[:count]}
