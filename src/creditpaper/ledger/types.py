from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class QueryScope(BaseModel):
    """One pair of clocks shared by every part of a retrieval request."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    valid_at: AwareDatetime
    known_at: AwareDatetime
    include_undated: bool = False

    @classmethod
    def now(cls) -> QueryScope:
        # The ledger database owns knowledge time, including across worker hosts.
        from creditpaper.db import cursor

        with cursor() as cur:
            cur.execute("SELECT clock_timestamp()")
            instant = cur.fetchone()[0]
        return cls(valid_at=instant, known_at=instant, include_undated=True)


class FactPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered(self) -> FactPeriod:
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["accept", "reject"]
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=0)
    period: FactPeriod | None = None


class PaperSectionBlocker(BaseModel):
    """Safe, user-presentable reason an in-template paper section blocks approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    key: str
    number: str
    engine_section: int
    name: str
    reason: Literal["missing", "not_ready", "stale"]


class ApprovalDisclosure(BaseModel):
    """A non-blocking document gap acknowledged in the immutable approval record."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    kind: Literal["unplaced", "invalid", "archived"]
    document_name: str
    description: str


class ApprovalDocumentBlocker(BaseModel):
    """An active, assigned document still processing at approval time."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    document_name: str


class LedgerError(Exception):
    """Safe, fixed error text only; no fact values or database details."""


class NotFound(LedgerError):
    pass


class Conflict(LedgerError):
    pass


class Hold(LedgerError):
    pass


class ApprovalDisclosuresNotAcknowledged(Hold):
    """The analyst must acknowledge the exact disclosures captured for approval."""

    code = "APPROVAL_DISCLOSURES_NOT_ACKNOWLEDGED"

    def __init__(self, outstanding_items: list[ApprovalDisclosure]) -> None:
        self.outstanding_items = outstanding_items
        super().__init__("Review and acknowledge the outstanding document disclosures before approval.")


class ApprovalContradictionsNotAcknowledged(Hold):
    """The analyst must resolve blocking conflicts and acknowledge other open entries."""

    code = "APPROVAL_CONTRADICTIONS_OPEN"

    def __init__(self, contradictions: list[dict[str, Any]]) -> None:
        self.open_contradictions = contradictions
        super().__init__("Review the open contradiction register before approval.")


class ApprovalDocumentsNotReady(Hold):
    """Approval refused only for documents currently assigned and awaiting extraction."""

    code = "APPROVAL_DOCUMENTS_NOT_READY"

    def __init__(self, blocking_items: list[ApprovalDocumentBlocker]) -> None:
        self.blocking_items = blocking_items
        super().__init__("Assigned documents must finish extraction before approval.")


class PaperSectionsNotReady(Hold):
    """Approval refused until all engine-backed selected-template sections are ready."""

    code = "PAPER_SECTIONS_NOT_READY"

    def __init__(self, blocking_items: list[PaperSectionBlocker]) -> None:
        self.blocking_items = blocking_items
        super().__init__("Selected template sections must be ready and current before approval.")


class ApprovalGuardrailsNotSignedOff(Hold):
    """A failed rule labelled block needs an explicit human sign-off before approval."""

    code = "APPROVAL_GUARDRAILS_NOT_SIGNED_OFF"

    def __init__(self, blocking_items: list[dict[str, Any]]) -> None:
        self.blocking_items = blocking_items
        super().__init__("Sign off each failed blocking guardrail before approval.")


RECOMMENDATION_REQUIRED_MESSAGE = "Approval needs a signed recommendation in Section 9."


class RecommendationRequired(Hold):
    """D8: approval refused because Section 9 has no signed, human-written recommendation."""

    code = "RECOMMENDATION_REQUIRED"


class BadInput(LedgerError, ValueError):
    pass


def identifier(value: str | UUID) -> str:
    return str(UUID(str(value)))
