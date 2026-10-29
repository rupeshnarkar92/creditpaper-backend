"""Expose the model's citable facts as {path: value} — the inverse of the
citation resolver. Section agents are handed this map so they can only cite what
exists (D4); the renderer and debugging use it too.

Walks the DealModel, emitting a dotted path (citation grammar) for every Fact
that is present and citable (extracted / confirmed / flagged — i.e. has
provenance), with a plain-text rendering of its value. Missing facts are omitted.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

from creditpaper.model.citations import CITABLE
from creditpaper.model.core import Fact
from creditpaper.model.deal import DealModel

# fields that are not deal facts (skip to keep the citable set clean):
_SKIP_TOP = {
    "prose",
    "outstanding_items",
    "model_version",
    "schema_version",
    "tenant_id",
    "deal_id",
    "created_at",
}


def _fmt(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _walk(obj: object, path: str, out: dict[str, str]) -> None:
    if isinstance(obj, Fact):
        if obj.value is not None and obj.status in CITABLE:
            out[path] = _fmt(obj.value)
        return
    if isinstance(obj, BaseModel) and hasattr(type(obj), "model_fields"):
        for name in type(obj).model_fields:
            child = f"{path}.{name}" if path else name
            _walk(getattr(obj, name), child, out)
        return
    if isinstance(obj, dict):
        for key, val in obj.items():
            _walk(val, f"{path}[{key}]", out)
        return
    if isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            _walk(item, f"{path}[{i}]", out)


def citable_facts(model: DealModel) -> dict[str, str]:
    """All present, citable facts as {dotted_path: value}."""
    out: dict[str, str] = {}
    for name in type(model).model_fields:
        if name in _SKIP_TOP:
            continue
        _walk(getattr(model, name), name, out)
    return out
