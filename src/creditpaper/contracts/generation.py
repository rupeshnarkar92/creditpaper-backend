"""Typed contracts for the generation harness (spec §8–§10, §12).

These cross the engine boundary: the pipeline produces them, the platform
stores and renders them. Additive to contract 2.0, hence 2.1.

Identity rule: a finding's id is minted from the knowledge base's standing
question and the one fact it is about (`subject_path`), never from the paths
the model happened to cite — cited paths vary run to run, and an id built from
them made the same finding look new on every run and let a strength and a
concern on the same fact collide.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from creditpaper.contracts.base import DealBase


class _Closed(DealBase):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class FindingKind(StrEnum):
    JUDGEMENT = "JUDGEMENT"
    COMPUTATION = "COMPUTATION"
    DIVERGENCE = "DIVERGENCE"
    COMPOUND = "COMPOUND"


class Direction(StrEnum):
    STRENGTH = "STRENGTH"
    NEUTRAL = "NEUTRAL"
    CONCERN = "CONCERN"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AnswerStatus(StrEnum):
    ANSWERED = "ANSWERED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    OPEN = "OPEN"


class EvidenceTier(StrEnum):
    SIGNED = "SIGNED"
    THIRD_PARTY = "THIRD_PARTY"
    BORROWER = "BORROWER"
    ANALYST = "ANALYST"
    COMPUTED = "COMPUTED"


class MitigantQuality(StrEnum):
    REAL = "REAL"
    ASPIRATIONAL = "ASPIRATIONAL"


class PredicateOp(StrEnum):
    LT = "LT"
    GT = "GT"
    APPROX = "APPROX"
    WITHIN = "WITHIN"


class BlockType(StrEnum):
    PARAGRAPH = "paragraph"
    KEY_FIGURES = "key_figures"
    RISK_ROW = "risk_row"
    REPAYMENT_TIERS = "repayment_tiers"
    STRESS_TABLE = "stress_table"
    OPEN_ITEM = "open_item"
    SCAFFOLD = "scaffold"


class TermKind(StrEnum):
    CP = "CP"
    CONTROL = "CONTROL"
    PRICING_VIEW = "PRICING_VIEW"


class RunVerdict(StrEnum):
    COMPLETE = "complete"
    COMPLETE_WITH_FLAGS = "complete_with_flags"
    DEGRADED = "degraded"
    FALLBACK_DELIVERED = "fallback_delivered"
    UNVERIFIED = "unverified"


def mint_finding_id(domain: str, question_id: str, subject_path: str) -> str:
    digest = hashlib.sha256(f"{domain}\x1f{question_id}\x1f{subject_path}".encode()).hexdigest()
    return "f_" + digest[:16]


class Predicate(_Closed):
    subject_ref: str
    op: PredicateOp
    object_ref: str | None = None
    band_ref: str | None = None
    basis: str


class Mitigation(_Closed):
    finding_id: str
    quality: MitigantQuality


class Finding(_Closed):
    # Minted in code from (domain, question_id, subject_path): a model cannot
    # compute it, and whatever it supplies is replaced.
    id: str = ""
    domain: str
    question_id: str
    topic: str
    subject_path: str
    kind: FindingKind
    predicate: Predicate | None = None
    observation: str
    direction: Direction
    severity: Severity
    evidence_tier: EvidenceTier
    implication: str
    mitigates: list[Mitigation] = Field(default_factory=list)
    cited_paths: list[str] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _mint_id(cls, data: object) -> object:
        if isinstance(data, dict) and {"domain", "question_id", "subject_path"} <= data.keys():
            data = dict(data)
            data["id"] = mint_finding_id(
                str(data["domain"]), str(data["question_id"]), str(data["subject_path"])
            )
        return data


class Answer(_Closed):
    question_id: str
    status: AnswerStatus
    reason: str | None = None
    checked_paths: list[str] = Field(default_factory=list)
    checked_docs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _invariants(self) -> Answer:
        if self.status is AnswerStatus.NOT_APPLICABLE and not self.reason:
            raise ValueError("NOT_APPLICABLE needs a reason")
        if self.status is AnswerStatus.OPEN and not (self.checked_paths or self.checked_docs):
            raise ValueError("an OPEN answer must record what was checked")
        return self


class OpenItem(_Closed):
    question_id: str | None = None
    statement: str
    checked_paths: list[str] = Field(default_factory=list)
    checked_docs: list[str] = Field(default_factory=list)


class Contribution(_Closed):
    domain: str
    skill_version: str
    answers: list[Answer]
    findings: list[Finding]
    open_items: list[OpenItem]

    @classmethod
    def from_model_reply(cls, data: dict[str, Any]) -> tuple[Contribution, list[str]]:
        """Validate a specialist's reply finding by finding (spec §8: invalid
        findings are dropped and logged, the domain is not). Returns the
        contribution and one message per dropped finding."""
        from pydantic import ValidationError

        kept: list[Finding] = []
        dropped: list[str] = []
        for i, raw in enumerate(data.get("findings") or []):
            try:
                kept.append(Finding.model_validate(raw))
            except ValidationError as exc:
                fields = sorted({str(err["loc"][0]) for err in exc.errors() if err.get("loc")})
                dropped.append(f"finding {i} dropped: invalid {', '.join(fields) or 'shape'}")
        rest = {k: v for k, v in data.items() if k != "findings"}
        return cls.model_validate({**rest, "findings": kept}), dropped


class Condition(_Closed):
    id: str
    statement: str
    crux_ref: str | None = None
    cited_paths: list[str] = Field(default_factory=list)


class AgainstItem(_Closed):
    finding_id: str
    answer: str


class ProposedTerm(_Closed):
    kind: TermKind
    statement: str
    controls_finding_id: str | None = None
    differs_from_its: bool = False


class Thesis(_Closed):
    proposition: str
    conditions: list[Condition]
    against: list[AgainstItem]
    proposed_terms: list[ProposedTerm] = Field(default_factory=list)


class PlanLink(_Closed):
    finding_ids: list[str]
    argument: str


class PlannedSection(_Closed):
    template_key: str
    point: str
    lead: str | None = None
    supports: list[str] = Field(default_factory=list)
    links: list[PlanLink] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    open_items: list[str] = Field(default_factory=list)
    budget_words: int
    scaffold: bool = False


class PaperPlan(_Closed):
    through_line: str
    sections: list[PlannedSection]
    omitted: list[str] = Field(default_factory=list)


class Block(_Closed):
    type: BlockType
    text: str
    finding_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _invariants(self) -> Block:
        if self.type in (BlockType.OPEN_ITEM, BlockType.SCAFFOLD) and self.finding_ids:
            raise ValueError("open items and scaffolds make no finding claims")
        return self


class SectionDraft(_Closed):
    template_key: str
    blocks: list[Block]
