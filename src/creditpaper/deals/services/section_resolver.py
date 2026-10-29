"""Interim section resolver: which engine section writes each template section.

Hidden from the analyst (spec 2026-09-15 §3). Deterministic, not a model call: it only
decides where existing engine output is shown and adds no facts, so D3 and D4 are
unaffected. When domain experts land, the backend replaces this adapter; the
paper-sections response shape does not change.

  - A section carrying `engine_section` (the Standard template) maps to that number.
  - Otherwise its heading is matched against the shared keyword map
    (`contracts.sections.SECTION_KEYWORDS`) in engine-section order; the first
    engine section whose keyword matches and that is not already used wins.
  - Each engine section is used at most once; unmatched sections map to None
    (`not_written`).
"""

from __future__ import annotations

import re
from typing import Any

from creditpaper.contracts.sections import SECTION_KEYWORDS, SECTION_NUMBERS

_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = tuple(
    (n, re.compile(SECTION_KEYWORDS[n], re.IGNORECASE)) for n in sorted(SECTION_KEYWORDS)
)


def map_sections(sections: list[dict[str, Any]]) -> dict[str, int | None]:
    """`{section key: engine section number | None}` for a snapshot's sections."""
    used: set[int] = set()
    mapping: dict[str, int | None] = {}
    # Explicit numbers are claimed first so a heading match earlier in the list
    # cannot take a number an explicit section owns.
    for s in sections:
        engine = s.get("engine_section")
        if isinstance(engine, int) and not isinstance(engine, bool) and engine in SECTION_NUMBERS:
            if engine not in used:
                used.add(engine)
                mapping[str(s["key"])] = engine
    for s in sections:
        key = str(s["key"])
        if key in mapping:
            continue
        name = str(s.get("name") or "")
        hit: int | None = None
        for number, pattern in _PATTERNS:
            if number not in used and pattern.search(name):
                hit = number
                break
        if hit is not None:
            used.add(hit)
        mapping[key] = hit
    return mapping


def engine_section_for_key(sections: list[dict[str, Any]], key: str) -> int | None:
    return map_sections(sections).get(key)
