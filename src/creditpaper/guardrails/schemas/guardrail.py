"""Guardrail schemas — Pydantic request/response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["wording", "structure", "always_do", "always_flag", "outside_appetite"]
Severity = Literal["info", "warn", "block"]
ScopeType = Literal["global", "module"]


class GuardrailCreate(BaseModel):
    """Request body for creating a firm rule. Fixed parts per kind only.

    Accepts the parts nested under `payload` (preferred) or flat at the
    top level (direct API callers); flat fields are merged into payload.
    """

    model_config = ConfigDict(extra="allow")

    kind: Kind
    payload: dict[str, Any] = Field(default_factory=dict)
    severity: Severity = "info"
    weight: float = Field(default=1.0, gt=0, le=10)
    scope_type: ScopeType = "global"
    scope_value: str | None = Field(default=None, max_length=64)


class GuardrailUpdate(BaseModel):
    """Partial update — same fixed parts, all optional."""

    model_config = ConfigDict(extra="allow")

    payload: dict[str, Any] | None = None
    severity: Severity | None = None
    weight: float | None = Field(default=None, gt=0, le=10)
    scope_type: ScopeType | None = None
    scope_value: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None


class GuardrailOut(BaseModel):
    id: str
    kind: str
    payload: dict[str, Any]
    severity: str
    weight: float
    scope_type: str
    scope_value: str | None
    is_active: bool
    created_at: str | None
    updated_at: str | None


class GuardrailEvaluationOut(BaseModel):
    id: str
    rule_id: str
    subject_type: str
    subject_id: str
    result: str
    measured: dict[str, Any] | None
    evaluated_at: str | None


class GuardrailRuleScore(BaseModel):
    rule_id: str
    kind: str
    evaluations: int
    passes: int
    flags: int
    pass_rate: float | None


class GuardrailsAccuracy(BaseModel):
    overall_score: float | None
    total_evaluations: int
    rules: list[GuardrailRuleScore]


class PlatformRule(BaseModel):
    key: str
    label: str


class GuardrailsListResponse(BaseModel):
    platform: list[PlatformRule]
    rules: list[GuardrailOut]
