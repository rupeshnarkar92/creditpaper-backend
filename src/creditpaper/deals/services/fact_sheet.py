"""The deal's fact sheet (contract 2026-09-15 §B): every fact in the model, grouped the way a
credit analyst reads, with what is required for this facility type and where it comes from.

Read-only (D1): the model snapshot is read, never written. D4: a fact is shown with its
provenance or as missing; nothing is inferred to fill a gap.

ONE layout source: `deals/fact_sheet/layout.json`. `required` and `expected_from` are
derived at read time from the slot taxonomy (a field is required when the layout marks it
core for the deal type AND a non-optional slot for that type feeds its block).
"""

from __future__ import annotations

import datetime
import json
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from creditpaper.deals.services.fact_walk import CITABLE_STATUSES, cites, is_fact
from creditpaper.deals.services.slot_taxonomy import (
    DEAL_TYPE_REQUIRED_MESSAGE,
    SlotType,
    is_facility_type,
    slot_type_from_row,
    slots_for,
)
from creditpaper.deals.store import slot_store

LAYOUT_PATH = Path(__file__).resolve().parents[1] / "fact_sheet" / "layout.json"

DERIVED_SOURCE_NAME = "Derived from the deal model"
HUMAN_ENTRY_SOURCE_NAME = "Analyst entry"

_ACRONYMS = {
    "prsv",
    "lvr",
    "lcr",
    "qs",
    "da",
    "ppsr",
    "ato",
    "aml",
    "pep",
    "gfa",
    "nla",
    "oc",
    "eoi",
    "sop",
    "its",
    "rfi",
    "fy",
}


class DealNotFound(Exception):
    """404 DEAL_NOT_FOUND."""


class DealTypeRequired(Exception):
    """409 DEAL_TYPE_REQUIRED."""

    message = DEAL_TYPE_REQUIRED_MESSAGE


@lru_cache(maxsize=1)
def load_layout() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))
    return data


# --- paths -------------------------------------------------------------------------

_SEGMENT = re.compile(r"^([a-z][a-z0-9_]*)(?:\[([^\]]+)\])?$")


def parse_path(path: str) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for part in path.split("."):
        m = _SEGMENT.match(part)
        if not m:
            raise ValueError(f"malformed layout path: {path}")
        out.append((m.group(1), m.group(2)))
    return out


def _flat(path: str) -> list[str]:
    """`rates[land_tax].cleared` -> ["rates", "land_tax", "cleared"]."""
    segs: list[str] = []
    for name, key in parse_path(path):
        segs.append(name)
        if key is not None:
            segs.append(key)
    return segs


def block_feeds(field_path: str, block: str) -> bool:
    """Whether a taxonomy `model_block` ("sponsor.structure", "rates.land_tax") holds the field.
    A `*` in the field path matches any key."""
    field_segs, block_segs = _flat(field_path), block.replace("[", ".").replace("]", "").split(".")
    if len(block_segs) > len(field_segs):
        return False
    return all(f == b or f == "*" for f, b in zip(field_segs, block_segs, strict=False))


def humanise(key: str) -> str:
    words = re.split(r"[_\s]+", str(key).strip())
    return " ".join(w.upper() if w.lower() in _ACRONYMS else w[:1].upper() + w[1:] for w in words if w)


# --- display -----------------------------------------------------------------------


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _plain(d: Decimal) -> str:
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _grouped(d: Decimal, places: int | None = None) -> str:
    if places is not None:
        return f"{d:,.{places}f}"
    whole, _, frac = _plain(abs(d)).partition(".")
    text = f"{int(whole):,}" + (f".{frac}" if frac else "")
    return ("-" if d < 0 else "") + text


def raw_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def display_value(value: object, kind: str, *, enum: bool = False) -> str | None:
    """Server-side formatting: AUD money "$18,500,000", percent "7.25%", dates "12 Mar 2026",
    months "18 months", booleans "Yes"/"No"."""
    if value is None:
        return None
    if kind == "money":
        d = _decimal(value)
        if d is None:
            return str(value)
        places = 2 if d != d.to_integral_value() else 0
        return ("-" if d < 0 else "") + "$" + _grouped(abs(d), places)
    if kind == "percent":
        d = _decimal(value)
        return f"{_plain(d)}%" if d is not None else str(value)
    if kind == "number":
        d = _decimal(value)
        return _grouped(d) if d is not None else str(value)
    if kind == "months":
        d = _decimal(value)
        if d is None:
            return str(value)
        return f"{_plain(d)} month" + ("" if d == 1 else "s")
    if kind == "date":
        try:
            day = value if isinstance(value, datetime.date) else datetime.date.fromisoformat(str(value)[:10])
        except ValueError:
            return str(value)
        return f"{day.day} {day.strftime('%b')} {day.year}"
    if kind == "boolean":
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return str(value)
    if isinstance(value, dict):
        rows = value.get("rows")
        return f"Table ({len(rows)} rows)" if isinstance(rows, list) else "Table"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return humanise(str(value)) if enum else str(value)


# --- sources -----------------------------------------------------------------------


class _Sources:
    def __init__(self, documents: list[dict[str, Any]], slot_names_by_document: dict[str, str]) -> None:
        self.documents = documents
        self.slot_names = slot_names_by_document

    def source(self, provenance: object) -> dict[str, Any] | None:
        if not isinstance(provenance, dict):
            return None
        ref = provenance.get("document_id")
        method = provenance.get("method")
        base = {"page": provenance.get("page"), "location": provenance.get("location"), "method": method}
        if method == "derived" or ref == "(derived)":
            return {"document_id": None, "document_name": DERIVED_SOURCE_NAME, **base, "page": None}
        for doc in self.documents:
            if cites(ref, doc):
                name = self.slot_names.get(doc["document_id"]) or doc.get("display_name")
                return {"document_id": doc["document_id"], "document_name": name, **base}
        if method == "human_entry":
            return {"document_id": None, "document_name": HUMAN_ENTRY_SOURCE_NAME, **base}
        tail = str(ref).split(":", 1)[1] if ref and ":" in str(ref) else ref
        return {"document_id": None, "document_name": tail, **base}


# --- requirement -------------------------------------------------------------------


def _core_for(field: dict[str, Any], deal_type: str) -> bool:
    core = field.get("core")
    if core is True:
        return True
    return isinstance(core, list) and deal_type in core


def feeding_slots(
    field_path: str, deal_type: str, slots: list[SlotType], layout: dict[str, Any]
) -> list[SlotType]:
    scoped = layout.get("type_scoped_blocks") or {}
    out = []
    for slot in slots:
        for block in slot.blocks():
            top = block.split(".")[0]
            if top in scoped and deal_type not in scoped[top]:
                continue
            if block_feeds(field_path, block):
                out.append(slot)
                break
    return sorted(out, key=lambda s: s.optional(deal_type))


def is_required(field: dict[str, Any], deal_type: str, slots: list[SlotType], layout: dict[str, Any]) -> bool:
    if not _core_for(field, deal_type):
        return False
    return any(not s.optional(deal_type) for s in feeding_slots(field["path"], deal_type, slots, layout))


def group_applies(group: dict[str, Any], deal_type: str) -> bool:
    applies = group.get("applies_to") or ["all"]
    return "all" in applies or deal_type in applies


# --- resolution against the snapshot -----------------------------------------------


def _render(segs: list[tuple[str, str | None]], stars: bool = False) -> str:
    return ".".join(n if k is None else f"{n}[{'*' if stars else k}]" for n, k in segs)


def _fixed_keys(layout: dict[str, Any]) -> dict[str, set[str]]:
    """For each `[*]` position, the keys a sibling fixed field already covers.

    Keyed by `<collection prefix>|<rest of the path>`, so `rates[land_tax].cleared` hides
    `land_tax` from `rates[*].cleared` but not from `rates[*].issue_date`.
    """
    fixed: dict[str, set[str]] = {}
    for group in layout["groups"]:
        for field in group["fields"]:
            segs = parse_path(field["path"])
            for i, (name, key) in enumerate(segs):
                if key in (None, "*"):
                    continue
                prefix = _render(segs[:i] + [(name, None)], stars=True)
                rest = _render(segs[i + 1 :], stars=True)
                fixed.setdefault(f"{prefix}|{rest}", set()).add(str(key))
    return fixed


def expand(model: dict[str, Any] | None, pattern: str, fixed: dict[str, set[str]]) -> list[dict[str, Any]]:
    """Concrete instances of a layout path in the snapshot:
    [{path, keys, fact | None, empty_collection: path | None}]."""
    segs = parse_path(pattern)
    results: list[dict[str, Any]] = []

    def _walk(node: object, i: int, path: str, keys: list[str]) -> None:
        if i == len(segs):
            results.append(
                {"path": path, "keys": keys, "fact": node if is_fact(node) else None, "empty": None}
            )
            return
        name, key = segs[i]
        child_path = f"{path}.{name}" if path else name
        child = node.get(name) if isinstance(node, dict) else None
        if key is None:
            _walk(child, i + 1, child_path, keys)
            return
        if key != "*":
            value = child.get(key) if isinstance(child, dict) else None
            _walk(value, i + 1, f"{child_path}[{key}]", keys)
            return
        skip = fixed.get(
            f"{_render(segs[:i] + [(name, None)], stars=True)}|{_render(segs[i + 1 :], stars=True)}", set()
        )
        entries = {k: v for k, v in child.items() if k not in skip} if isinstance(child, dict) else {}
        if not entries:
            results.append({"path": child_path, "keys": keys, "fact": None, "empty": child_path})
            return
        for k, v in entries.items():
            _walk(v, i + 1, f"{child_path}[{k}]", [*keys, str(k)])

    _walk(model or {}, 0, "", [])
    return results


def field_label(path: str) -> str:
    """The fact sheet's label for one concrete field path (`rates[land_tax].cleared`), for
    Activity. A fixed-key layout entry wins over its `[*]` pattern; a path the layout does
    not list is labelled from its segments."""
    try:
        segs = parse_path(path)
    except ValueError:
        return humanise(path.replace(".", " "))
    stars = _render(segs, stars=True)
    fields = [f for g in load_layout()["groups"] for f in g["fields"]]
    exact = next((f for f in fields if f["path"] == path), None)
    if exact is not None:
        return _label(exact, [])
    pattern = next((f for f in fields if f["path"] == stars), None)
    if pattern is not None:
        return _label(pattern, [str(k) for _, k in segs if k is not None])
    name, key = segs[-1]
    return humanise(name) if key is None else f"{humanise(name)} ({humanise(key)})"


def _label(field: dict[str, Any], keys: list[str]) -> str:
    label = str(field["label"])
    if keys:
        label = label.replace("{key}", humanise(keys[0]))
        if len(keys) > 1:
            label = (
                label.replace("{key2}", humanise(keys[1]))
                if "{key2}" in label
                else f"{label} ({humanise(keys[1])})"
            )
    return label


# --- the sheet ---------------------------------------------------------------------


def _fact_row(
    *,
    path: str,
    label: str,
    kind: str,
    fact: dict[str, Any] | None,
    required: bool,
    expected_from: list[str],
    sources: _Sources,
    enum: bool = False,
) -> dict[str, Any]:
    if fact is None or fact.get("status") not in CITABLE_STATUSES:
        return {
            "path": path,
            "label": label,
            "value": None,
            "display_value": None,
            "kind": kind,
            "status": "missing",
            "required": required,
            "confidence": None,
            "source": None,
            "expected_from": expected_from,
            "conflict": None,
        }
    value = fact.get("value")
    source = sources.source(fact.get("provenance"))
    shown = display_value(value, kind, enum=enum)
    status = str(fact["status"])
    return {
        "path": path,
        "label": label,
        "value": raw_value(value),
        "display_value": shown,
        "kind": kind,
        "status": status,
        "required": required,
        "confidence": fact.get("confidence"),
        "source": source,
        "expected_from": expected_from,
        # Only the held value is stored on a flagged fact; the diverging reading is not
        # persisted by the apply layer yet, so the conflict lists what the model holds.
        "conflict": {"values": [{"display_value": shown, "source": source}]} if status == "flagged" else None,
    }


def _subtree_rows(
    model: dict[str, Any] | None, field: dict[str, Any], sources: _Sources
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for inst in expand(model, field["path"], {}):
        if inst["empty"]:
            continue
        node = _resolve(model, inst["path"])

        def _walk(n: object, rel: str, base: str = inst["path"], keys: list[str] = inst["keys"]) -> None:
            if is_fact(n):
                assert isinstance(n, dict)
                if n.get("status") in CITABLE_STATUSES:
                    tail = rel.replace(".", " ").replace("[", " ").replace("]", "")
                    label = f"{_label(field, keys)}: {humanise(tail)}"
                    kind = _guess_kind(rel, n.get("value"))
                    rows.append(
                        _fact_row(
                            path=f"{base}.{rel}",
                            label=label,
                            kind=kind,
                            fact=n,
                            required=False,
                            expected_from=[],
                            sources=sources,
                        )
                    )
                return
            if isinstance(n, dict):
                for k, v in n.items():
                    _walk(v, f"{rel}.{k}" if rel else str(k))

        _walk(node, "")
    return rows


def _resolve(model: dict[str, Any] | None, path: str) -> object:
    node: object = model or {}
    for name, key in parse_path(path):
        node = node.get(name) if isinstance(node, dict) else None
        if key is not None:
            node = node.get(key) if isinstance(node, dict) else None
    return node


def _guess_kind(rel: str, value: object) -> str:
    last = rel.split(".")[-1]
    if isinstance(value, bool):
        return "boolean"
    if last.endswith("_pct") or last == "pct":
        return "percent"
    if last.endswith("date") or last.startswith("period_"):
        return "date"
    if last in {"land_area", "gfa_nla", "site_area", "gfa_total"} or rel.startswith("methodology.figures"):
        return "number"
    if _decimal(value) is not None and not isinstance(value, str | bool):
        return "money"
    if (
        isinstance(value, str)
        and re.fullmatch(r"-?\d+(\.\d+)?", value)
        and last in {"sale_price", "sum", "amount"}
    ):
        return "money"
    return "text"


def build_fact_sheet(
    *,
    deal_id: str,
    deal_type: str,
    model: dict[str, Any] | None,
    slot_types: list[SlotType],
    documents: list[dict[str, Any]],
    slot_names_by_document: dict[str, str],
    layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    layout = layout or load_layout()
    slots = slots_for(deal_type, slot_types)
    sources = _Sources(documents, slot_names_by_document)
    fixed = _fixed_keys(layout)
    groups_out = []
    counts = {"confirmed": 0, "extracted": 0, "flagged": 0, "missing_required": 0}

    extracted_type = (_resolve(model, "deal.type") if model else None) or None
    type_conflict = (
        isinstance(extracted_type, dict)
        and extracted_type.get("status") in CITABLE_STATUSES
        and extracted_type.get("value") not in (None, deal_type)
    )

    for group in layout["groups"]:
        if not group_applies(group, deal_type):
            continue
        rows: list[dict[str, Any]] = []
        emitted_empty: set[str] = set()  # one missing row per empty collection per group
        if group["key"] == "deal" and type_conflict:
            assert isinstance(extracted_type, dict)
            read_source = sources.source(extracted_type.get("provenance"))
            rows.append(
                {
                    "path": "deal.type",
                    "label": "Facility Type",
                    "value": deal_type,
                    "display_value": humanise(deal_type),
                    "kind": "text",
                    "status": "flagged",
                    "required": False,
                    "confidence": None,
                    "source": None,
                    "expected_from": [],
                    "conflict": {
                        "values": [
                            {"display_value": humanise(deal_type), "source": None},
                            {
                                "display_value": humanise(str(extracted_type.get("value"))),
                                "source": read_source,
                            },
                        ]
                    },
                }
            )
        for field in group["fields"]:
            if field["kind"] == "subtree":
                rows += _subtree_rows(model, field, sources)
                continue
            if field["path"] == "deal.type" and type_conflict:
                continue
            required = is_required(field, deal_type, slots, layout)
            expected = [s.name for s in feeding_slots(field["path"], deal_type, slots, layout)]
            for inst in expand(model, field["path"], fixed):
                fact = inst["fact"]
                present = fact is not None and fact.get("status") in CITABLE_STATUSES
                if not present and not required:
                    continue
                if inst["empty"]:
                    if inst["empty"] in emitted_empty or "*" not in field["path"]:
                        continue
                    emitted_empty.add(inst["empty"])
                    collection = field["path"].split("[*]")[0]
                    if inst["empty"] != collection:
                        # An outer entry exists but its inner collection is empty.
                        label = f"{_label(field, inst['keys'])}"
                    else:
                        label = field.get("collection_label") or humanise(collection.split(".")[-1])
                    rows.append(
                        _fact_row(
                            path=inst["empty"],
                            label=label,
                            kind=field["kind"],
                            fact=None,
                            required=required,
                            expected_from=expected,
                            sources=sources,
                        )
                    )
                    continue
                rows.append(
                    _fact_row(
                        path=inst["path"],
                        label=_label(field, inst["keys"]),
                        kind=field["kind"],
                        fact=fact,
                        required=required,
                        expected_from=expected,
                        sources=sources,
                        enum=bool(field.get("enum")),
                    )
                )
        if not rows:
            continue
        for row in rows:
            if row["status"] == "missing":
                counts["missing_required"] += 1
            else:
                counts[row["status"]] = counts.get(row["status"], 0) + 1
        groups_out.append({"key": group["key"], "title": group["title"], "facts": rows})

    return {
        "deal_id": deal_id,
        "deal_type": deal_type,
        "model_version": (model or {}).get("model_version") if model else None,
        "summary": counts,
        "groups": groups_out,
    }


def get_fact_sheet(tenant_id: str, deal_id: str) -> dict[str, Any]:
    raw = slot_store.call_fact_sheet_data(tenant_id, deal_id)
    return fact_sheet_from_raw(deal_id, raw)


def fact_sheet_from_raw(deal_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Pure fact-sheet response builder for stored-procedure payloads."""
    deal = raw.get("deal")
    if deal is None:
        raise DealNotFound(deal_id)
    deal_type = deal.get("deal_type")
    if not is_facility_type(deal_type):
        raise DealTypeRequired(deal_id)
    assert isinstance(deal_type, str)
    slot_types = [slot_type_from_row(r) for r in raw["slot_types"]]
    documents = raw["documents"]
    names = _slot_names_by_document(raw["slots"], raw["files"], slot_types)
    return build_fact_sheet(
        deal_id=deal_id,
        deal_type=deal_type,
        model=raw["model"],
        slot_types=slot_types,
        documents=documents,
        slot_names_by_document=names,
    )


def _slot_names_by_document(
    rows: list[dict], files: list[dict], slot_types: list[SlotType]
) -> dict[str, str]:
    """Document id -> the name of the slot it was confirmed (or proposed) into. Read-only:
    the fact sheet never creates slot rows."""
    names = {s.key: s.name for s in slot_types}
    slot_rows = {r["id"]: r for r in rows}
    out: dict[str, str] = {}
    ranked = sorted(
        (f for f in files if f["role"] == "assigned"),
        key=lambda f: slot_rows.get(f["slot_id"], {}).get("status") != "confirmed",
    )
    for f in ranked:
        row = slot_rows.get(f["slot_id"])
        if row and f["document_id"] not in out and row["slot_key"] in names:
            out[f["document_id"]] = names[row["slot_key"]]
    return out
