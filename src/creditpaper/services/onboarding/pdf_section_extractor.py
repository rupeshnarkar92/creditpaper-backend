"""PDF section extractor.

Public surface:
  - extract_sections(content: bytes) -> list[dict]
    Returns [{"number": "01", "name": "Executive Summary"}, ...]
    Numbered from 1, top 10. Empty list on any failure. A section may carry
    ``subsections`` ([{"number": "1.1", "name": ...}]), omitted when there are none.

Algorithm (per docs/superpowers/specs/2026-08-30-wizard-shakeup-spec.md):
  1. Read the PDF's outline (pypdfium2, ``ingest/reader.read_toc``); if it
     yields rows, format those (level-1 rows are sections, level-2 rows under
     them are subsections, trailing page numbers stripped, numbered "01".."NN").
  2. Otherwise walk every page (pdfplumber), take the largest font size used on
     at least two pages as section headings, and the next size down (still
     clearly larger than body text, also on two pages) as subsection headings.
     Top 10.
  3. Never raise — return ``[]`` on any decode error so the caller can persist
     the document row.

PyMuPDF was replaced on 2026-10-07 (AGPL; DECISIONS).
"""

from __future__ import annotations

import io
import re
from typing import Any

_TRAILING_PAGE = re.compile(r"\s*\d+\s*$")


def _strip_trailing_page(title: str) -> str:
    return _TRAILING_PAGE.sub("", title).strip()


_MAX_SECTIONS = 10
_MAX_SUBSECTIONS = 30


def _add_subsection(section: dict, name: str) -> None:
    """Append a subsection numbered within its section, skipping repeats."""
    subs = section.setdefault("subsections", [])
    key = " ".join(name.lower().split())
    if not key or len(subs) >= _MAX_SUBSECTIONS:
        return
    if any(" ".join(s["name"].lower().split()) == key for s in subs):
        return
    subs.append({"number": f"{int(section['number'])}.{len(subs) + 1}", "name": name})


def _format_toc(toc: list) -> list[dict]:
    """Level-1 rows are sections; level-2 rows under one are its subsections.

    Each section keeps its start page under ``_page`` for preview lookup; the
    caller strips it before returning.
    """
    out: list[dict] = []
    for level, title, page in toc:
        name = _strip_trailing_page(str(title))
        if not name:
            continue
        if level == 1:
            if len(out) >= _MAX_SECTIONS:
                break
            out.append({"number": f"{len(out) + 1:02d}", "name": name, "_page": page})
        elif level == 2 and out:
            _add_subsection(out[-1], name)
    return out


def _is_header_or_footer(text: str, page_num: int, total_pages: int) -> bool:
    """Heuristic: reject text that looks like a header, footer or page number."""
    t = text.strip()
    if not t:
        return True
    # Page number (just digits, possibly with leading zeros)
    if re.fullmatch(r"\d{1,4}", t):
        return True
    # Very short alpha-only on non-first page likely a header element
    if len(t) <= 3 and t.isalpha():
        return True
    # "Page X of Y" or similar
    if re.fullmatch(r"\d+\s*/\s*\d+", t):
        return True
    return False


def _page_spans(page: Any) -> list[tuple[float, str]]:
    """A page's text as runs of one font size on one line, in reading order: ``(size, text)``.

    The unit the size analysis works on (what PyMuPDF called a span): words on
    the same line in the same size are one run, so a heading set in one size
    reads as one string."""
    words = page.extract_words(extra_attrs=["size"], use_text_flow=True, keep_blank_chars=False)
    spans: list[tuple[float, str]] = []
    run: list[str] = []
    run_size = run_top = None
    for word in words:
        size = round(float(word.get("size") or 0), 2)
        top = float(word.get("top") or 0)
        same_line = run_top is not None and abs(top - run_top) < max(2.0, 0.3 * size)
        if run and same_line and size == run_size:
            run.append(word["text"])
            continue
        if run and run_size:
            spans.append((run_size, " ".join(run)))
        run, run_size, run_top = [word["text"]], size, top
    if run and run_size:
        spans.append((run_size, " ".join(run)))
    return spans


def _format_by_font(content: bytes) -> list[dict]:
    """Extract section headings from font-size analysis.

    Strategy: collect every span with its font size, find the largest font
    size that appears on at least 2 distinct pages (likely section headings),
    and deduplicate by exact text. For each heading, also capture the body
    text that follows until the next heading.
    """
    import pdfplumber

    spans: list[tuple[int, float, str]] = []
    size_pages: dict[float, set[int]] = {}
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        total_pages = len(pdf.pages)
        for pn, page in enumerate(pdf.pages, start=1):
            for size, text in _page_spans(page):
                text = text.strip()
                if not text or size <= 0:
                    continue
                spans.append((pn, size, text))
                size_pages.setdefault(size, set()).add(pn)

    if not spans:
        return []

    candidate_sizes = sorted(
        [s for s, pages in size_pages.items() if len(pages) >= 2],
        reverse=True,
    )
    if not candidate_sizes:
        candidate_sizes = sorted(size_pages.keys(), reverse=True)

    target_size = candidate_sizes[0]

    # Body text is the size carrying the most characters. A subsection heading is
    # the next size down from section headings that is still clearly larger than
    # body text and used on at least two pages; otherwise there are none.
    chars_by_size: dict[float, int] = {}
    for _pn, size, text in spans:
        chars_by_size[size] = chars_by_size.get(size, 0) + len(text)
    body_size = max(chars_by_size, key=lambda s: chars_by_size[s])
    sub_size = next(
        (
            s
            for s in candidate_sizes
            if s < target_size - 0.5 and s > body_size + 0.9 and len(size_pages[s]) >= 2
        ),
        None,
    )

    seen: set[str] = set()
    picked: list[dict] = []
    current_body: list[str] = []
    current_heading: str | None = None
    current_subs: list[str] = []

    def _flush() -> None:
        if current_heading is None:
            return
        body = " ".join(current_body).strip()
        body = re.sub(r"\s+", " ", body)
        preview = body[:600].rstrip()
        if preview and not preview.endswith("."):
            preview = preview.rstrip() + "."
        section = {
            "number": f"{len(picked) + 1:02d}",
            "name": current_heading,
            "preview": preview,
        }
        for name in current_subs:
            _add_subsection(section, name)
        picked.append(section)

    for _pn, size, text in spans:
        if abs(size - target_size) < 0.5:
            normalized = _strip_trailing_page(text)
            if not normalized or normalized in seen:
                continue
            if _is_header_or_footer(normalized, _pn, total_pages):
                continue
            _flush()
            current_body = []
            current_subs = []
            current_heading = normalized
            seen.add(normalized)
        else:
            if (
                sub_size is not None
                and current_heading is not None
                and abs(size - sub_size) < 0.5
                and len(text) <= 120
            ):
                normalized = _strip_trailing_page(text)
                if normalized and not _is_header_or_footer(normalized, _pn, total_pages):
                    current_subs.append(normalized)
            if current_heading is not None and len(current_body) < 60:
                current_body.append(text)

    _flush()
    return picked[:_MAX_SECTIONS]


def _attach_previews_from_toc(content: bytes, sections: list[dict]) -> list[dict]:
    """For TOC-derived sections, attach a short preview from the page where
    each section starts.

    Uses each section's own start page. The page used to come from zipping the
    sections against the whole TOC, which drifted as soon as the TOC had any
    level-2 rows, so a preview could be taken from another section's page.
    """
    if not sections:
        return sections
    from creditpaper.ingest.reader import PdfiumReader

    enriched: list[dict] = []
    with PdfiumReader(content) as reader:
        for entry in sections:
            sec = {k: v for k, v in entry.items() if k != "_page"}
            page_num = entry.get("_page") or 1
            preview = ""
            try:
                text = reader.page(page_num).text or ""
                text = re.sub(r"\s+", " ", text).strip()
                preview = text[:600].rstrip()
                if preview and not preview.endswith("."):
                    preview = preview.rstrip() + "."
            except Exception:
                preview = ""
            enriched.append({**sec, "preview": preview})
    return enriched


def extract_sections(content: bytes) -> list[dict]:
    """Return the section list for the given PDF bytes, or ``[]`` on failure.

    Each entry: ``{"number": "01", "name": "Executive Summary", "preview": "..."}``.
    ``preview`` is up to 600 characters of body text immediately following the
    heading; empty string if no body was captured.
    """
    if not content:
        return []
    from creditpaper.ingest.reader import read_toc

    try:
        try:
            toc = read_toc(content)
        except Exception:
            toc = []
        if toc:
            sections = _attach_previews_from_toc(content, _format_toc(toc))
        else:
            sections = _format_by_font(content)
        for section in sections:
            if not section.get("subsections"):
                section.pop("subsections", None)
        return sections
    except Exception:
        return []
