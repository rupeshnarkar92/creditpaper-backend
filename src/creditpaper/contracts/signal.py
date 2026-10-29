"""HITL review signal — the append-only capture plane (LEARNING_SUBSTRATE.md stage 1).

Every comment or edit an analyst makes on generated section prose is an immutable
Signal: the raw material the offline edit-recovery replay/promotion engine (stage
2, not yet built) will later turn into Lessons. Capture does not wait on that
engine (DECISIONS.md 2026-07-10) — signals accumulate now; they aren't minable
into Lessons until stage 2 exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from pydantic import Field

from creditpaper.contracts.base import DealBase


class SignalKind(str, Enum):
    COMMENT = "comment"  # anchored remark, no text change (mirrors Word-comment review)
    EDIT = "edit"  # before/after text change on the section draft


class Signal(DealBase):
    id: str = Field(default_factory=lambda: uuid4().hex)
    tenant_id: str = "dev"
    deal_id: str
    section: int
    kind: SignalKind
    author: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # SignalKind.COMMENT
    comment_text: str | None = None
    anchored_text: str | None = None

    # SignalKind.EDIT
    before_text: str | None = None
    after_text: str | None = None
