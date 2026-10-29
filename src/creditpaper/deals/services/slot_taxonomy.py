"""Slot types and their resolution for a deal's facility type (contract 2026-09-15 §A).

Pure: no database, no HTTP. The slot types themselves are stored records
(`document_slot_types`, seeded by migration 0015 from docs/specs/slot-taxonomy); the
store reads them and hands them here as `SlotType`s.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEAL_TYPES = ("construction", "bridge", "residual_stock")
CATEGORY_ORDER = ("borrower", "property", "builder", "legal")

DEAL_TYPE_REQUIRED_MESSAGE = "Set this deal's facility type before its document list can be built."


@dataclass(frozen=True)
class SlotType:
    key: str
    name: str
    description: str | None
    category: str
    applies_to: tuple[str, ...]
    optional_for: tuple[str, ...]
    multi: bool
    extractor: str | None
    model_block: str | None
    checks: tuple[str, ...] = ()
    sort_order: int = 0

    def applies(self, deal_type: str) -> bool:
        return "all" in self.applies_to or deal_type in self.applies_to

    def optional(self, deal_type: str) -> bool:
        return "all" in self.optional_for or deal_type in self.optional_for

    def blocks(self) -> tuple[str, ...]:
        """The model blocks this slot feeds, e.g. ("economics", "its", "covenants")."""
        if not self.model_block:
            return ()
        return tuple(b.strip() for b in self.model_block.split(",") if b.strip())


@dataclass(frozen=True)
class RefusedClass:
    key: str
    name: str
    reason: str
    match: tuple[str, ...] = field(default_factory=tuple)
    decision: str | None = None


def slot_type_from_row(row: dict[str, Any]) -> SlotType:
    return SlotType(
        key=str(row["key"]),
        name=str(row["name"]),
        description=row.get("description"),
        category=str(row["category"]),
        applies_to=tuple(row.get("applies_to") or ()),
        optional_for=tuple(row.get("optional_for") or ()),
        multi=bool(row.get("multi")),
        extractor=row.get("extractor"),
        model_block=row.get("model_block"),
        checks=tuple(row.get("checks") or ()),
        sort_order=int(row.get("sort_order") or 0),
    )


def refused_class_from_row(row: dict[str, Any]) -> RefusedClass:
    return RefusedClass(
        key=str(row["key"]),
        name=str(row.get("name") or row["key"]),
        reason=str(row["reason"]),
        match=tuple(row.get("match") or ()),
        decision=row.get("decision"),
    )


def is_facility_type(deal_type: str | None) -> bool:
    return deal_type in DEAL_TYPES


def slots_for(deal_type: str, slot_types: list[SlotType]) -> list[SlotType]:
    """The slots a deal of this facility type needs, ordered by RFI category then seed order."""
    applicable = [s for s in slot_types if s.applies(deal_type)]
    return sorted(
        applicable,
        key=lambda s: (
            CATEGORY_ORDER.index(s.category) if s.category in CATEGORY_ORDER else len(CATEGORY_ORDER),
            s.sort_order,
        ),
    )


def required_slot_keys(deal_type: str, slot_types: list[SlotType]) -> set[str]:
    return {s.key for s in slots_for(deal_type, slot_types) if not s.optional(deal_type)}
