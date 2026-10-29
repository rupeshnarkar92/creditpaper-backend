"""Plan which pages an extractor actually reads, and never truncate.

A structural pass indexes the cached parse; each field group then gets windows
covering every page its hints point at. Two rules are load-bearing:

  * Nothing is dropped. A matched page set larger than one window is SPLIT into
    several windows, never cut. The team's earlier pipeline capped at 10 pages
    and 6,000 characters and silently lost a funding table on page 16; that is
    the failure this module exists to prevent. `processing/page_selector.py`
    already states the rule for one provider - "context overflow must be
    explicit, never silently discard evidence" - and this generalises it.
  * Shape follows the transport. With prompt caching the document is cached once
    and many small windows are cheap. Without it, every window re-sends its
    pages, so groups whose pages coincide are merged to avoid paying twice.

The hint phrases are NOT invented here. `page_selector._PATTERNS` holds 85 of
them, accumulated from real documents, and DEFAULT_GROUPS is seeded from it. A
second drifting copy of that knowledge would be worse than none.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from creditpaper.ingest.parse_cache import CachedParse
from creditpaper.llm.registry import Capability, ModelChoice, supports
from creditpaper.processing.page_selector import _PATTERNS

FIRST_PAGES_FALLBACK = 5


class FieldGroup(BaseModel):
    name: str
    hints: tuple[str, ...]


class PageIndex(BaseModel):
    headings: dict[int, str]
    word_counts: dict[int, int]


class Window(BaseModel):
    group: str
    pages: tuple[int, ...]
    reason: str


# One group per document kind the codebase already knows how to find pages for.
DEFAULT_GROUPS: tuple[FieldGroup, ...] = tuple(
    FieldGroup(name=kind, hints=tuple(phrases)) for kind, phrases in sorted(_PATTERNS.items()) if phrases
)


def build_index(parse: CachedParse) -> PageIndex:
    """The structural pass: where things are, without reading them closely.

    This is what `Extractor.structural_pass` is declared for - "optional pass 1
    for long documents: locate where things live".
    """
    headings: dict[int, str] = {}
    word_counts: dict[int, int] = {}
    for page, text in sorted(parse.pages.items()):
        word_counts[page] = len(text.split())
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        headings[page] = first[:120]
    return PageIndex(headings=headings, word_counts=word_counts)


def pages_for(parse: CachedParse, group: FieldGroup) -> tuple[int, ...]:
    """Pages whose text matches any of the group's hints; empty when none do.

    Takes the parse rather than the index: matching needs the page text, and a
    PageIndex deliberately holds only headings and word counts.
    """
    return tuple(
        page
        for page, text in sorted(parse.pages.items())
        if any(hint.casefold() in text.casefold() for hint in group.hints)
    )


def _chunk(pages: Sequence[int], size: int) -> list[tuple[int, ...]]:
    """Split, never cut: every page in `pages` lands in exactly one chunk."""
    return [tuple(pages[i : i + size]) for i in range(0, len(pages), size)]


def plan_windows(
    parse: CachedParse,
    groups: Sequence[FieldGroup],
    choice: ModelChoice,
    *,
    max_pages: int = 12,
) -> list[Window]:
    all_pages = tuple(sorted(parse.pages))
    fits = len(all_pages) <= max_pages

    matched: dict[str, tuple[int, ...]] = {}
    reasons: dict[str, str] = {}
    for group in groups:
        if fits:
            matched[group.name] = all_pages
            reasons[group.name] = "document fits in one window"
            continue
        hits = pages_for(parse, group)
        matched[group.name] = hits or all_pages[:FIRST_PAGES_FALLBACK]
        reasons[group.name] = "hint matched" if hits else "no hint matched; first pages read"

    if not supports(choice, Capability.PROMPT_CACHE):
        # Every call re-sends its pages, so two groups pointing at the same
        # pages would pay for them twice. Merging is the whole saving, and it
        # applies to a short document as much as a long one.
        merged: dict[tuple[int, ...], list[str]] = {}
        for name, pages in matched.items():
            merged.setdefault(pages, []).append(name)
        windows: list[Window] = []
        for pages, names in sorted(merged.items()):
            for chunk in _chunk(pages, max_pages):
                windows.append(
                    Window(
                        group="+".join(sorted(names)),
                        pages=chunk,
                        reason="merged: transport re-sends pages per call",
                    )
                )
        return windows

    windows = []
    for group in groups:
        for chunk in _chunk(matched[group.name], max_pages):
            windows.append(Window(group=group.name, pages=chunk, reason=reasons[group.name]))
    return windows


def pages_read(windows: Sequence[Window]) -> tuple[int, ...]:
    """Every page any window covers, so a gap is visible rather than assumed."""
    seen: set[int] = set()
    for window in windows:
        seen.update(window.pages)
    return tuple(sorted(seen))
