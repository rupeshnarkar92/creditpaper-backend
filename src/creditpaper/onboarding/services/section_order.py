"""The firm's chosen section order, laid over the sections merged from its papers."""

from __future__ import annotations

import re
from typing import Any


def _key(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def apply_saved_order(
    sections: list[dict[str, Any]], saved: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Sections in the saved order, renumbered from 01.

    Saved names that no longer come from any paper are dropped; sections found since the order
    was saved follow at the end, in the order the papers give them. No saved order: unchanged.
    """
    if not saved:
        return sections
    by_key = {_key(s.get("name")): s for s in sections}
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in saved:
        key = _key(item.get("name"))
        if key in by_key and key not in seen:
            ordered.append(by_key[key])
            seen.add(key)
    ordered.extend(s for s in sections if _key(s.get("name")) not in seen)
    return [{**s, "number": f"{i:02d}"} for i, s in enumerate(ordered, start=1)]
