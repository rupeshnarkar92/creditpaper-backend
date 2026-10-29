"""Shared section-list merge: union of papers' sections in upload order.

Moved here from the onboarding voice service so the template wizard can
reuse the identical rule. Pure function — no DB, no network.
"""

from __future__ import annotations

import re
from typing import Any


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def merge_section_lists(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge `[{filename, sections: [{number, name, subsections?}]}]` into one list.

    Upload order, deduped by normalized name, renumbered sequentially.
    Each row carries `source_filename`. All papers are equal — no primary.
    Subsections are unioned the same way under the section they share a name
    with, in the order first seen, and renumbered within it ("2.1", "2.2");
    `subsections` is present only when a section has any.
    """
    merged: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    sub_keys: dict[str, set[str]] = {}
    for d in docs:
        filename = d.get("filename", "")
        for s in d.get("sections") or []:
            name = s.get("name", "") or ""
            key = _key(name)
            if not key:
                continue
            row = by_key.get(key)
            if row is None:
                row = {
                    "number": f"{len(merged) + 1:02d}",
                    "name": name,
                    "source_filename": filename,
                }
                merged.append(row)
                by_key[key] = row
                sub_keys[key] = set()
            for sub in s.get("subsections") or []:
                sub_name = sub.get("name", "") or ""
                sub_key = _key(sub_name)
                if not sub_key or sub_key in sub_keys[key]:
                    continue
                sub_keys[key].add(sub_key)
                subs = row.setdefault("subsections", [])
                subs.append({"number": f"{int(row['number'])}.{len(subs) + 1}", "name": sub_name})
    return merged
