"""Rule-based page selector — Stage 2 of the extraction pipeline.

Given the S2 page_texts (already extracted, no LLM cost), identifies which pages
are relevant for each document_kind. PyMuPDF then renders ONLY those pages as PNGs
for the AI vision pass — instead of sending the whole PDF.

Design:
- Keyword sets per document_kind match against the page's extracted text.
- Returns a sorted, deduplicated list of 0-indexed page numbers.
- Always includes the first supplied page (normally the cover page).
- Caps at MAX_PAGES_PER_PASS to bound LLM context/cost.
- Falls back to the first MAX_PAGES_PER_PASS pages when no keywords fire
  (better than sending nothing, which causes the extractor to emit nothing).
"""

from __future__ import annotations

import logging
import re

from creditpaper.llm.providers import LLMProvider

log = logging.getLogger(__name__)

# Default cap for a default Anthropic extraction route. Per-call transport
# limits are chosen at selection time, after task and tenant routing resolve.
MAX_PAGES_PER_PASS = 10

# Keyword sets per document_kind.  All lowercase; matched against lowercased
# page text.  Longer / more specific phrases before short ones so the regex
# short-circuits on the first hit.
_PATTERNS: dict[str, list[str]] = {
    "its": [
        "indicative term sheet",
        "term sheet",
        "facility limit",
        "facility amount",
        "interest rate",
        "establishment fee",
        "line fee",
        "maturity date",
        "loan term",
        "drawdown",
        "borrower",
        "guarantor",
        "covenant",
        "lvr",
        "tranche",
    ],
    "valuation": [
        "market value",
        "as if complete",
        "as is value",
        "gross realisation",
        "net realisation",
        "site value",
        "land value",
        "capitalisation rate",
        "cap rate",
        "comparable sale",
        "registered valuer",
        "valuation date",
        "lvr",
    ],
    "da": [
        "development application",
        "development consent",
        "lapsing date",
        "conditions of consent",
        "consent authority",
        "council approval",
        "planning permit",
        "section 4.55",
        "modification",
        "approved gfa",
        "lot",
    ],
    "qs": [
        "quantity surveyor",
        "construction cost",
        "cost plan",
        "contingency",
        "gfa",
        "gross floor area",
        "cost per sqm",
        "practical completion",
        "lockup",
        "fixing",
        "rise and fall",
    ],
    "building_contract": [
        "building contract",
        "lump sum",
        "cost plus",
        "contract sum",
        "practical completion",
        "defects liability",
        "retention",
        "head contractor",
        "principal contractor",
        "pc sum",
    ],
    "sponsor_kyc": [
        "know your customer",
        "beneficial owner",
        "aml",
        "anti-money laundering",
        "sanctions",
        "pep",
        "politically exposed",
        "adverse media",
        "identity verified",
        "credit check",
        "director",
        "equifax",
        "illion",
        "veda",
    ],
    "insurance": [
        "policy schedule",
        "insured value",
        "sum insured",
        "premium",
        "excess",
        "construction all risk",
        "public liability",
        "policy number",
        "period of insurance",
        "expiry",
        "insurer",
    ],
    "other": [],  # no patterns — fallback applies
}

# Pages that carry a document's core schedule, kept ahead of the cap. The generic
# patterns above fire on nearly every page of a term sheet, so a first-N cap used
# to drop the funding table, which sits in a schedule at the back of the ITS.
_PRIORITY: dict[str, list[str]] = {
    "its": [
        "funding table",
        "sources and uses",
        "sources & uses",
        "application of funds",
        "uses of funds",
    ],
}

# A page with fewer than this many characters of extracted text has no usable
# text layer — it is a scan/photo, not selectable text. The keyword patterns
# above can never fire on it (there is ~nothing to match), so without this it
# would never be selected: the S2 text pass is silent, and the vision pass
# only sees the pages the (text-only) selector chose. Scanned pages fill
# whatever room is left after the named schedule keywords (_PRIORITY) and the
# generic keyword matches — real keyword hits are text someone can point to,
# a scanned page is a guess, so it only takes a cap slot once every matched
# page already has one. Exception: a document with no text layer at all has
# no keyword hits to lose to, so scanned pages are then taken from the start.
TEXT_LESS_CHAR_THRESHOLD = 20


def _selected_provider():
    from creditpaper.llm.registry import Task, resolve_provider

    return resolve_provider(Task.EXTRACT)


def _available_page_indices(page_texts: list[dict]) -> list[int]:
    pages: set[int] = set()
    for entry in page_texts:
        try:
            page = int(entry.get("page", 1) or 1)
        except (TypeError, ValueError):
            continue
        if page > 0:
            pages.add(page - 1)
    return sorted(pages)


def select_pages(
    page_texts: list[dict],
    document_kind: str,
    *,
    max_pages: int | None = None,
) -> list[int]:
    """Return 0-indexed page numbers relevant for ``document_kind``.

    Args:
        page_texts: List of dicts with at least ``{"page": int, "text": str}``,
                    as produced by the S2 pipeline (processing/pipeline.py).
                    Pages are 1-indexed in the dict but 0-indexed in the return value.
        document_kind: One of the keys in _PATTERNS; unknown kinds treated as "other".
        max_pages: Hard cap on pages returned.

    Returns:
        Sorted selected page numbers from the supplied page set. The first
        supplied page acts as the cover fallback; sparse windows never add pages
        outside the pages they were given. Length ≤ max_pages when capped.
    """
    if not page_texts:
        return []
    available_pages = _available_page_indices(page_texts)
    if not available_pages:
        return []
    available = set(available_pages)

    if max_pages is None:
        # Text inference has no vision-page budget. Keep every supplied page;
        # context overflow must be explicit, never silently discard evidence.
        provider = _selected_provider()
        if provider == LLMProvider.NVIDIA:
            return available_pages
        max_pages = 3 if provider == LLMProvider.OLLAMA else 10
    patterns = _PATTERNS.get(document_kind, [])

    # Compile patterns into a single alternation regex for speed.
    if patterns:
        regex = re.compile("|".join(re.escape(p) for p in patterns), re.IGNORECASE)
    else:
        regex = None

    first_page = available_pages[0]
    relevant: set[int] = {first_page}
    priority: set[int] = set()
    textless: set[int] = set()
    prio = _PRIORITY.get(document_kind, [])
    prio_regex = re.compile("|".join(re.escape(p) for p in prio), re.IGNORECASE) if prio else None

    for entry in page_texts:
        try:
            page_idx = int(entry.get("page", 1) or 1) - 1
        except (TypeError, ValueError):
            continue
        if page_idx not in available:
            continue
        text = entry.get("text", "")
        next_page = page_idx + 1
        if len(text.strip()) < TEXT_LESS_CHAR_THRESHOLD:
            textless.add(page_idx)
        if regex and regex.search(text):
            relevant.add(page_idx)
            # A page break companion is included only if it belongs to the
            # supplied set (important for windowed/sparse reads).
            if next_page in available:
                relevant.add(next_page)
        if prio_regex and prio_regex.search(text):
            priority.add(page_idx)
            if next_page in available:
                priority.add(next_page)

    # Cap: first supplied page, named-schedule priority, keyword matches, then
    # text-less scanned pages. A fully scanned document takes supplied pages
    # from the start; no contiguity assumption is made for any of these sets.
    keep = [first_page] + sorted(p for p in priority if p != first_page and p in available)
    keep += [p for p in sorted(relevant) if p not in keep and p in available]
    if len(textless) >= len(available):  # not one supplied page has a text layer
        keep += [p for p in available_pages if p not in keep]
    else:
        keep += [p for p in sorted(textless) if p not in keep and p in available]
    if regex is None and not prio_regex:
        # An unknown kind has no patterns, so use the opening of the supplied set.
        keep += [p for p in available_pages if p not in keep]
    sorted_pages = sorted(keep[:max_pages])

    # Fallback: no keyword matched, so read the first supplied pages.
    if sorted_pages == [first_page] and len(available_pages) > 1:
        sorted_pages = available_pages[:max_pages]

    return [p for p in sorted_pages if p in available]


# Ported from the devel_jason line on merge (2026-09-23). Both ingestion
# models are kept while the merged one is measured, so a surface only one
# line had is carried rather than dropped.
def select_pages_detail(
    page_texts: list[dict],
    document_kind: str,
    *,
    max_pages: int | None = None,
) -> tuple[list[int], bool]:
    """Return 0-indexed page numbers relevant for ``document_kind``.

    Args:
        page_texts: List of dicts with at least ``{"page": int, "text": str}``,
                    as produced by the S2 pipeline (processing/pipeline.py).
                    Pages are 1-indexed in the dict but 0-indexed in the return value.
        document_kind: One of the keys in _PATTERNS; unknown kinds treated as "other".
        max_pages: Hard cap on pages returned.

    Returns:
        (pages, capped). `pages` is a sorted list of 0-indexed page numbers
        from the supplied pages. `capped` says whether the vision-page budget
        dropped pages that matched - the caller is reading part of a document,
        and must be able to know it.

    A returned `capped=True` is the difference between a bounded read and a
    silent one. The earlier pipeline capped at ten pages and lost a funding
    table on page 16 without a word; `ingest/windows.py` splits instead of
    cutting for text transports, and this budget exists only because a vision
    pass pays per rendered page. Sparse page lists are expected in the windowed
    path; page numbers stay in original-document coordinates and are never
    clamped by the number of entries in the list.
    """
    if not page_texts:
        return [], False
    available_pages = _available_page_indices(page_texts)
    if not available_pages:
        return [], False
    available = set(available_pages)

    if max_pages is None:
        # Text inference has no vision-page budget. Keep every supplied page;
        # context overflow must be explicit, never silently discard evidence.
        provider = _selected_provider()
        if provider == LLMProvider.NVIDIA:
            return available_pages, False
        max_pages = 3 if provider == LLMProvider.OLLAMA else 10
    patterns = _PATTERNS.get(document_kind, [])

    # Compile patterns into a single alternation regex for speed.
    if patterns:
        regex = re.compile("|".join(re.escape(p) for p in patterns), re.IGNORECASE)
    else:
        regex = None

    first_page = available_pages[0]
    relevant: set[int] = {first_page}

    for entry in page_texts:
        try:
            page_idx = int(entry.get("page", 1) or 1) - 1
        except (TypeError, ValueError):
            continue
        if page_idx not in available:
            continue
        text = entry.get("text", "")
        if regex and regex.search(text):
            relevant.add(page_idx)
            # Include the following page only when this read supplied it.
            if page_idx + 1 in available:
                relevant.add(page_idx + 1)

    # Sort and cap the actual supplied page numbers, then remember that it capped.
    ordered = sorted(relevant & available)
    sorted_pages = ordered[:max_pages]
    capped = len(ordered) > len(sorted_pages)

    # Fallback: no keyword matched, so use the first supplied pages.
    if sorted_pages == [first_page] and len(available_pages) > 1:
        sorted_pages = available_pages[:max_pages]

    pages = [p for p in sorted_pages if p in available]
    if capped:
        log.warning(
            "page selection capped at %d of %d matching pages for kind %s",
            len(pages),
            len(ordered),
            document_kind,
        )
    return pages, capped
