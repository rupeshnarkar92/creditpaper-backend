"""Persisted state machine for onboarding paper processing.

State lives in the store, never only in worker memory: a worker restart must not
lose a paper mid-pipeline (spec §5). `progress` is derived from the state's
position in STAGE_ORDER rather than stored separately, so the two can never
disagree.
"""

from __future__ import annotations

from enum import Enum


class PaperStatus(str, Enum):
    UPLOADED = "UPLOADED"
    SCANNING = "SCANNING"
    EXTRACTING = "EXTRACTING"
    QUALITY_CHECK = "QUALITY_CHECK"
    CLASSIFYING = "CLASSIFYING"
    APPROVAL_CHECK = "APPROVAL_CHECK"
    STRUCTURE_ANALYSIS = "STRUCTURE_ANALYSIS"
    FACT_EXTRACTION = "FACT_EXTRACTION"
    FACT_VALIDATION = "FACT_VALIDATION"
    TAGGING = "TAGGING"
    STYLE_ANALYSIS = "STYLE_ANALYSIS"
    FINAL_VALIDATION = "FINAL_VALIDATION"
    VALIDATED = "VALIDATED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class ApprovalStatus(str, Enum):
    APPROVED = "APPROVED"
    CONDITIONALLY_APPROVED = "CONDITIONALLY_APPROVED"
    RECOMMENDED_ONLY = "RECOMMENDED_ONLY"
    DECLINED = "DECLINED"
    UNKNOWN = "UNKNOWN"


class StageResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    PROCESSING = "PROCESSING"
    PENDING = "PENDING"
    SKIPPED = "SKIPPED"


# Only these count toward the three-paper gate (spec §13, §21). RECOMMENDED_ONLY
# is deliberately excluded: a recommendation is not a committee decision.
COUNTING_APPROVAL_STATUSES: frozenset[ApprovalStatus] = frozenset(
    {ApprovalStatus.APPROVED, ApprovalStatus.CONDITIONALLY_APPROVED}
)

# Pipeline order. Progress percent is this list's index, so adding a stage
# reweights the bar automatically and no percentage is invented anywhere.
STAGE_ORDER: tuple[PaperStatus, ...] = (
    PaperStatus.UPLOADED,
    PaperStatus.SCANNING,
    PaperStatus.EXTRACTING,
    PaperStatus.QUALITY_CHECK,
    PaperStatus.CLASSIFYING,
    PaperStatus.APPROVAL_CHECK,
    PaperStatus.STRUCTURE_ANALYSIS,
    PaperStatus.FACT_EXTRACTION,
    PaperStatus.FACT_VALIDATION,
    PaperStatus.TAGGING,
    PaperStatus.STYLE_ANALYSIS,
    PaperStatus.FINAL_VALIDATION,
)

TERMINAL_STATUSES: frozenset[PaperStatus] = frozenset(
    {
        PaperStatus.VALIDATED,
        PaperStatus.NEEDS_REVIEW,
        PaperStatus.REJECTED,
        PaperStatus.FAILED,
    }
)

# A permanent verdict must not be retried into a different answer; only FAILED
# (an infrastructure or transient fault) is recoverable.
RETRYABLE_STATUSES: frozenset[PaperStatus] = frozenset({PaperStatus.FAILED})


def is_terminal(status: PaperStatus | str) -> bool:
    return PaperStatus(status) in TERMINAL_STATUSES


def is_retryable(status: PaperStatus | str) -> bool:
    return PaperStatus(status) in RETRYABLE_STATUSES


def progress_for(status: PaperStatus | str) -> int:
    """0-100. Terminal states are 100; in-flight states are their stage position."""
    s = PaperStatus(status)
    if s in TERMINAL_STATUSES:
        return 100
    try:
        idx = STAGE_ORDER.index(s)
    except ValueError:
        return 0
    return int(round((idx / len(STAGE_ORDER)) * 100))


def counts_toward_gate(status: PaperStatus | str, approval: ApprovalStatus | str | None) -> bool:
    """The gate rule in one place, so API, store and worker cannot disagree."""
    if PaperStatus(status) is not PaperStatus.VALIDATED:
        return False
    if approval is None:
        return False
    return ApprovalStatus(approval) in COUNTING_APPROVAL_STATUSES
