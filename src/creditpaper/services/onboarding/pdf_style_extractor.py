"""PDF style extractor.

Public surface:
  - extract_style(content: bytes) -> dict
    Returns ``{"font_heading": str|None, "font_body": str|None,
               "primary_color": "#RRGGBB"|None}``.

Approach (per docs/superpowers/specs/2026-08-30-wizard-shakeup-spec.md):
  - font_heading: the font face used by the largest-size text on page 1.
  - font_body: the most common font face on page 1, excluding the heading.
  - primary_color: the most-saturated non-greyscale colour found in the
    drawings of page 1 (sample coloured rectangles, text, etc.).

Read through pdfplumber (PyMuPDF replaced 2026-10-07, DECISIONS). The function
never raises: on any decode or parse failure it returns
``{"font_heading": None, "font_body": None, "primary_color": None}``.
"""

from __future__ import annotations

import collections
import io
import re
from collections.abc import Iterable, Sequence
from typing import Any

# A subset-embedded font is named "ABCDEF+Arial-BoldMT"; the face is after the "+".
_SUBSET_TAG = re.compile(r"^[A-Z]{6}\+")


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _face(fontname: str | None) -> str:
    return _SUBSET_TAG.sub("", fontname or "")


def _heading_and_body_fonts(chars: Sequence[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Return (heading_font, body_font) from page 1's characters, or (None, None).

    Heading = font face of the largest-size text on the page.
    Body = the most common font face that isn't the heading.
    """
    spans = [
        (round(float(c.get("size") or 0), 2), _face(c.get("fontname")))
        for c in chars
        if (c.get("text") or "").strip()
    ]
    if not spans:
        return None, None
    spans.sort(key=lambda s: s[0])
    heading_size, heading_font = spans[-1]
    heading = heading_font or None
    body_counts: collections.Counter[str] = collections.Counter()
    for size, font in spans:
        if font and not (size == heading_size and font == heading_font):
            body_counts[font] += 1
    body_font = body_counts.most_common(1)[0][0] if body_counts else None
    return heading, body_font


def _rgb(value: object) -> tuple[float, float, float] | None:
    """A pdfplumber colour as RGB floats: RGB as is, CMYK converted, greyscale and patterns ignored."""
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, (int, float)) for v in value):
        return None
    if len(value) == 3:
        return float(value[0]), float(value[1]), float(value[2])
    if len(value) == 4:
        c, m, y, k = (float(v) for v in value)
        return (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
    return None


def _saturated_primary(objects: Iterable[dict[str, Any]]) -> str | None:
    """Most-saturated non-greyscale colour among page 1's drawn objects, as ``#RRGGBB``, or None."""
    best: tuple[float, tuple[int, int, int]] | None = None
    for item in objects:
        # Colours can appear in several slots; combine stroke + fill.
        for slot in ("stroking_color", "non_stroking_color"):
            rgb = _rgb(item.get(slot))
            if rgb is None:
                continue
            r, g, b = rgb
            # Skip greyscale-ish colours (max - min < 0.15).
            if max(r, g, b) - min(r, g, b) < 0.15:
                continue
            # Skip very low intensity (treat as greyscale).
            if max(r, g, b) < 0.20:
                continue
            sat = max(r, g, b) - min(r, g, b)
            as_bytes = (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))
            if best is None or sat > best[0]:
                best = (sat, as_bytes)
    return None if best is None else _hex(best[1])


def extract_style(content: bytes) -> dict:
    """Return the style dict for the given PDF bytes; all-None on failure."""
    empty = {"font_heading": None, "font_body": None, "primary_color": None}
    if not content:
        return empty
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(content)) as pdf:
            if not pdf.pages:
                return empty
            page = pdf.pages[0]
            heading, body = _heading_and_body_fonts(page.chars)
            primary = _saturated_primary([*page.rects, *page.curves, *page.lines, *page.chars])
            return {"font_heading": heading, "font_body": body, "primary_color": primary}
    except Exception:
        return empty
