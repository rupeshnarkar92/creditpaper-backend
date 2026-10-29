"""Find documents hidden inside one PDF, and refuse to guess where they start.

Nearly half of the fixture corpus's multi-page PDFs hold more than one document,
and every PDF over 150 pages does. Left undetected, a valuation buried at page 60
of a scanned bundle never reaches a slot and never becomes a fact.

Detection splits into two regimes, and the difference is stark enough to be a
design rule rather than a tuning parameter. Where pages carry "Page 1 of N"
style numbering, boundary detection scores F1 0.84 - good enough to act on.
Where they do not, it scores 0.40, which would shred single documents into
fragments. So a restart in page numbering splits; anything else raises a review
finding and leaves the document whole.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

_PAGE_OF = re.compile(r"\bpage\s+(\d{1,3})\s+of\s+(\d{1,3})\b", re.I)
_COVER_RE = re.compile(
    r"\b(report|certificate|agreement|contract|statement|valuation|invoice|schedule|deed)\b", re.I
)


class BundleFinding(BaseModel):
    is_bundle: bool
    boundaries: list[int]
    confirmed_by_page_numbering: bool
    needs_review: bool


def _page_marker(text: str) -> tuple[int, int] | None:
    match = _PAGE_OF.search(text or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _looks_like_a_cover(text: str) -> bool:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False
    head = lines[0]
    return bool(_COVER_RE.search(head)) and (head.isupper() or len(head) < 80)


def detect_boundaries(pages: dict[int, str]) -> BundleFinding:
    if len(pages) < 2:
        return BundleFinding(
            is_bundle=False, boundaries=[], confirmed_by_page_numbering=False, needs_review=False
        )

    numbers = sorted(pages)
    markers = {n: _page_marker(pages[n]) for n in numbers}
    numbered = [n for n in numbers if markers[n] is not None]
    has_numbering = len(numbered) >= max(2, len(numbers) // 2)

    if has_numbering:
        boundaries: list[int] = []
        previous: tuple[int, int] | None = None
        for n in numbers:
            marker = markers[n]
            if marker is None:
                continue
            # A restart to "page 1" after an earlier marker starts a new
            # document. The bundle's own first page is not a boundary.
            if previous is not None and marker[0] == 1 and n != numbers[0]:
                boundaries.append(n)
            previous = marker
        return BundleFinding(
            is_bundle=bool(boundaries),
            boundaries=boundaries,
            confirmed_by_page_numbering=True,
            needs_review=False,
        )

    suspected = [n for n in numbers[1:] if _looks_like_a_cover(pages[n])]
    return BundleFinding(
        is_bundle=bool(suspected),
        boundaries=[],
        confirmed_by_page_numbering=False,
        needs_review=bool(suspected),
    )
