"""Deal lifecycle state machine.

The state machine lives in the domain layer, not in the frontend. Every
state transition is recorded in `deal_state_transitions` and mirrored to
`deal_audit_events` in the same database transaction so the audit log
cannot drift from the truth.

`ISSUED` and `ARCHIVED` are terminal in this slice; amendments to an
issued paper come in a later slice.
"""

from __future__ import annotations

LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"INTAKE", "ARCHIVED"}),
    "INTAKE": frozenset({"DRAFT", "PROCESSING", "ARCHIVED"}),
    "PROCESSING": frozenset({"INTAKE", "FACT_REVIEW", "ARCHIVED"}),
    "FACT_REVIEW": frozenset({"INTAKE", "PAPER_GENERATED", "ARCHIVED"}),
    "PAPER_GENERATED": frozenset({"FACT_REVIEW", "PAPER_EDITING", "PENDING_APPROVAL", "ARCHIVED"}),
    "PAPER_EDITING": frozenset({"PAPER_GENERATED", "PAPER_REVIEW", "PENDING_APPROVAL", "ARCHIVED"}),
    "PAPER_REVIEW": frozenset({"PAPER_EDITING", "PENDING_APPROVAL", "ARCHIVED"}),
    "PENDING_APPROVAL": frozenset({"PAPER_REVIEW", "APPROVED", "REJECTED", "ARCHIVED"}),
    "APPROVED": frozenset({"ISSUED", "PENDING_APPROVAL", "ARCHIVED"}),
    "REJECTED": frozenset({"DRAFT", "ARCHIVED"}),
    "ISSUED": frozenset(),
    "ARCHIVED": frozenset(),
}


def is_legal(from_state: str, to_state: str) -> bool:
    return to_state in LEGAL_TRANSITIONS.get(from_state, frozenset())


def allowed_from(state: str) -> list[str]:
    return sorted(LEGAL_TRANSITIONS.get(state, frozenset()))
