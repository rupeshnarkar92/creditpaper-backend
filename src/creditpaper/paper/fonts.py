"""The curated, self-hosted font set a paper theme may use. Pure, no I/O.

The backend is the source of truth for which families exist; the web app maps these
family names to its bundled @fontsource packages. Nothing is fetched from a third-party
font host at runtime (visitor IPs would reach that host; LG München I, 3 O 17493/20).

Scraped family names are matched loosely and case-insensitively ("Source Sans Pro" is
Source Sans 3, "IBM Plex" variants are Plex). No match: the default family for the
scraped font's category (serif or sans), else the default for the role.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Category = Literal["serif", "sans"]

_SERIF_STACK = '"{family}", Georgia, "Times New Roman", serif'
_SANS_STACK = '"{family}", Arial, Helvetica, sans-serif'


@dataclass(frozen=True)
class FontEntry:
    family: str
    category: Category
    docx: str  # a family Word reliably has

    @property
    def stack(self) -> str:
        template = _SERIF_STACK if self.category == "serif" else _SANS_STACK
        return template.format(family=self.family)

    def as_theme(self) -> dict[str, str]:
        return {"family": self.family, "stack": self.stack, "docx": self.docx}


FONT_SET: tuple[FontEntry, ...] = (
    FontEntry("Source Serif 4", "serif", "Georgia"),
    FontEntry("Source Sans 3", "sans", "Arial"),
    FontEntry("Inter", "sans", "Arial"),
    FontEntry("IBM Plex Sans", "sans", "Arial"),
    FontEntry("IBM Plex Serif", "serif", "Georgia"),
    FontEntry("Lora", "serif", "Georgia"),
    FontEntry("Merriweather", "serif", "Georgia"),
    FontEntry("Montserrat", "sans", "Arial"),
    FontEntry("Open Sans", "sans", "Arial"),
    FontEntry("Roboto", "sans", "Arial"),
    FontEntry("Playfair Display", "serif", "Georgia"),
    FontEntry("Libre Franklin", "sans", "Arial"),
)

_BY_FAMILY = {f.family.lower(): f for f in FONT_SET}

# Matched against the family name lowercased with everything but letters and digits
# removed ("IBM Plex Sans" -> "ibmplexsans"). Ordered: first rule that matches wins,
# so the more specific rules come first.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p), fam)
    for p, fam in (
        (r"^sourceserif", "Source Serif 4"),
        (r"^sourcesans", "Source Sans 3"),
        (r"^(ibm)?plexserif", "IBM Plex Serif"),
        (r"^(ibm)?plexsans|^ibmplex$|^plex$", "IBM Plex Sans"),
        (r"^inter(var|variable|display|ui)?$", "Inter"),
        (r"^lora", "Lora"),
        (r"^merriweather(?!sans)", "Merriweather"),
        (r"^montserrat", "Montserrat"),
        (r"^opensans", "Open Sans"),
        (r"^roboto(?!slab|mono|serif)", "Roboto"),
        (r"^playfair", "Playfair Display"),
        (r"^librefranklin", "Libre Franklin"),
    )
)

_SERIF_HINTS = ("georgia", "times", "garamond", "baskerville", "caslon", "didot", "bodoni", "slab")
_SANS_HINTS = ("arial", "helvetica", "verdana", "segoe", "system-ui", "grotesk", "grotesque", "gothic")


def font_entry(family: str) -> FontEntry | None:
    return _BY_FAMILY.get(family.strip().lower())


def _families(scraped: str) -> list[str]:
    """A CSS font-family value split into family names, quotes removed."""
    return [p.strip().strip("'\"").strip() for p in scraped.split(",") if p.strip().strip("'\"").strip()]


def match_font(scraped: object) -> FontEntry | None:
    """The curated family a scraped name (or CSS stack) refers to, or None."""
    if not isinstance(scraped, str) or not scraped.strip():
        return None
    for name in _families(scraped):
        compact = re.sub(r"[^a-z0-9]", "", name.lower())
        if not compact:
            continue
        for pattern, family in _RULES:
            if pattern.search(compact):
                return _BY_FAMILY[family.lower()]
    return None


def guess_category(scraped: object) -> Category | None:
    """serif / sans from generic keywords in a scraped name or stack, else None."""
    if not isinstance(scraped, str):
        return None
    low = scraped.lower()
    if "sans" in low:  # before "serif": "sans-serif" contains both
        return "sans"
    if "serif" in low or any(h in low for h in _SERIF_HINTS):
        return "serif"
    if any(h in low for h in _SANS_HINTS):
        return "sans"
    return None


def resolve_font(scraped: object, *, role_default: str, category_defaults: dict[str, str]) -> FontEntry:
    """Scraped family -> curated entry; unmatched -> the category default; unknown
    category -> the role default. Never raises for bad input."""
    hit = match_font(scraped)
    if hit is not None:
        return hit
    category = guess_category(scraped)
    if category is not None:
        entry = font_entry(category_defaults.get(category, ""))
        if entry is not None:
            return entry
    entry = font_entry(role_default)
    if entry is not None:
        return entry
    # A default record naming a family outside the set: fall back to the set itself
    # rather than failing a request over theme data.
    return FONT_SET[0]
