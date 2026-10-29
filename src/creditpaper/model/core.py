"""Deal-model fact primitives.

Every fact in the deal model is a Fact: value + provenance + confidence + status.
Provenance is written at parse time by the extractor (D4). No anchor, no fact —
a Fact cannot be constructed in `extracted` status without provenance.

(`Fact`, not `Field`: the earlier name shadowed `pydantic.Field`, which every
extractor instance also imports. Renamed before Wave 1 — DECISIONS.md 2026-06-11.)
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import AwareDatetime, Field, model_validator

from creditpaper.contracts.base import DealBase

# `ingest.values` imports nothing from the package: the GST basis is defined once,
# where spans are read, and the model records it.
from creditpaper.ingest.values import GstBasis

T = TypeVar("T")


class FactStatus(str, Enum):
    EXTRACTED = "extracted"
    CONFIRMED = "confirmed"
    FLAGGED = "flagged"
    MISSING = "missing"


class ExtractionMethod(str, Enum):
    MAPPED_READER = "mapped_reader"  # deterministic readers
    LLM_EXTRACTION = "llm_extraction"
    HUMAN_ENTRY = "human_entry"
    DERIVED = "derived"  # computed from other anchored facts; provenance records the formula


class EvidenceTier(str, Enum):
    """How far a fact can be trusted by who asserted it (schema 0.11, DECISIONS 2026-10-07).

    Derived from the source document's slot (`model/evidence.py`), never guessed."""

    SIGNED_LEGAL = "signed_legal"  # executed or legal documents: ITS, contracts, deeds, approvals
    INDEPENDENT = "independent"  # a third party with no stake: valuer, QS, insurer, screening, authority
    BORROWER_ASSERTED = "borrower_asserted"  # the borrower's own statements and schedules
    ANALYST_CONFIRMED = "analyst_confirmed"  # entered or confirmed by the lender's analyst
    COMPUTED = "computed"  # derived in code from other facts


def _unset(value: Any) -> bool:
    return value is None


class Provenance(DealBase):
    """Structural anchor: where a fact came from, written at parse time.

    For DERIVED facts the anchor is a computation, not a page/clause: `location`
    holds the formula (e.g. "equity = total - senior") and `inputs` lists the
    field paths it was computed from, so the value stays auditable (D4) and a
    check can re-run the derivation to confirm it (tamper-evident)."""

    document_id: str
    page: int | None = None
    location: str  # page/sheet anchor, or — for DERIVED — the formula
    method: ExtractionMethod
    inputs: list[str] = []  # DERIVED only: field paths this value was computed from
    document_version_id: str | None = None
    source_system: str | None = None
    extraction_run_id: str | None = None
    extractor_version: str | None = None
    model_id: str | None = None
    input_assertion_ids: list[str] = []
    # Schema 0.10 (DECISIONS 2026-09-18/19). Omitted from dumps while unset, so a
    # stored assertion written before them keeps its fingerprint.
    # The concept registry version the fact was extracted under (model/concepts.py).
    registry_version: str | None = Field(default=None, exclude_if=_unset)
    # The extraction prompt's version, for the label store (spec 3c).
    prompt_version: str | None = Field(default=None, exclude_if=_unset)
    # Schema 0.11: who asserted it, from the source document's slot. Omitted while unset.
    evidence_tier: EvidenceTier | None = Field(default=None, exclude_if=_unset)


class FactBasis(DealBase):
    """The qualifiers that make a figure comparable (spec 3a). A statement is only
    compared with another of the same concept when these are compatible. None means
    not recorded; ``GstBasis.UNSTATED`` means read, and the span states no basis."""

    gst: GstBasis | None = None
    valuation_basis: str | None = None  # as_is / as_if_complete / ...
    as_at: datetime.date | None = None  # the date the figure speaks at
    currency: str | None = None  # ISO code, e.g. AUD
    period: str | None = None  # p.a. / monthly / a financial year label


class VerificationSummary(DealBase):
    """What verification concluded about a fact (``ingest/verify.py::FactVerification``),
    kept on the fact so a decision on it is labelled with the verdict it was shown."""

    verdict: str
    reasons: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1)


class Fact(DealBase, Generic[T]):
    value: T | None = None
    provenance: Provenance | None = None
    confidence: float | None = None  # extractor-assigned, 0–1
    status: FactStatus = FactStatus.MISSING
    assertion_id: str | None = None
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None
    # Schema 0.10. Omitted from dumps while unset (see Provenance).
    basis: FactBasis | None = Field(default=None, exclude_if=_unset)
    verification: VerificationSummary | None = Field(default=None, exclude_if=_unset)
    # Schema 0.11: the value or its clause reads as instructions aimed at a model
    # (`ingest/injection.py`). Data only; changes neither status nor confidence.
    injection_suspected: bool | None = Field(default=None, exclude_if=_unset)

    @model_validator(mode="after")
    def _no_anchor_no_fact(self) -> Fact[T]:
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if self.status in (FactStatus.EXTRACTED, FactStatus.CONFIRMED) and self.provenance is None:
            raise ValueError(
                f"Fact with status '{self.status.value}' requires provenance (D4: no anchor, no field)"
            )
        return self


# ProseBlock, its enums and the D8 guard live in `creditpaper.contracts`: they
# cross the engine/platform boundary — the review surface renders, edits and posts
# signals against them. Re-exported here because the engine has always imported
# them from `model.core`, and because `Fact` (which does NOT cross) sits beside
# them in every engine call site.
from creditpaper.contracts.prose import (  # noqa: E402
    Authorship,
    ProseBlock,
    SectionStatus,
    machine_recommendations,
)

__all__ = [
    "Authorship",
    "ExtractionMethod",
    "Fact",
    "FactBasis",
    "FactStatus",
    "GstBasis",
    "ProseBlock",
    "Provenance",
    "SectionStatus",
    "VerificationSummary",
    "machine_recommendations",
]
