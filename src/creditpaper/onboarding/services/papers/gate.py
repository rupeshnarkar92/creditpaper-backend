"""The three-paper gate — one authority, enforced server-side.

Spec §21: the front end may render the count, but it may not decide it. Every
caller (papers API, onboarding state, step transition) reads the gate from here,
so a client cannot advance onboarding by asserting it is allowed to.
"""

from __future__ import annotations

from typing import Any

from creditpaper.onboarding.services.papers.config import REQUIRED_VALIDATED_PAPERS
from creditpaper.onboarding.services.papers.states import counts_toward_gate
from creditpaper.onboarding.utils.papers_store import list_papers


def counting_papers(tenant_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in list_papers(tenant_id)
        if counts_toward_gate(row.get("status", ""), row.get("approval_status"))
    ]


def validated_approved_count(tenant_id: str) -> int:
    return len(counting_papers(tenant_id))


def can_continue(tenant_id: str) -> bool:
    return validated_approved_count(tenant_id) >= REQUIRED_VALIDATED_PAPERS


def progress(tenant_id: str) -> dict[str, Any]:
    """The shape `GET /onboarding/papers/progress` returns."""
    rows = list_papers(tenant_id)
    validated = validated_approved_count(tenant_id)
    return {
        "total": len(rows),
        "validated_count": validated,
        "required": REQUIRED_VALIDATED_PAPERS,
        "can_continue": validated >= REQUIRED_VALIDATED_PAPERS,
    }
