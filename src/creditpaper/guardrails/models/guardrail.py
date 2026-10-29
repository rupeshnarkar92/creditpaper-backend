"""Dataclass row models for guardrails.

Mirrors the templates pattern — raw SQL lives in store/guardrail_store.py;
these are typed representations of rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class GuardrailRule:
    """One row of the `guardrail_rules` table."""

    id: str
    tenant_id: str
    kind: str
    payload: dict[str, Any]
    severity: str
    weight: float
    scope_type: str
    scope_value: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> GuardrailRule:
        return cls(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            kind=row["kind"],
            payload=dict(row.get("payload") or {}),
            severity=row.get("severity", "info"),
            weight=float(row.get("weight", 1.0)),
            scope_type=row.get("scope_type", "global"),
            scope_value=row.get("scope_value"),
            is_active=bool(row.get("is_active", True)),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "payload": self.payload,
            "severity": self.severity,
            "weight": self.weight,
            "scope_type": self.scope_type,
            "scope_value": self.scope_value,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class GuardrailEvaluation:
    """One row of the `guardrail_evaluations` table."""

    id: str
    tenant_id: str
    rule_id: str
    subject_type: str
    subject_id: str
    result: str
    measured: dict[str, Any] | None
    evaluated_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> GuardrailEvaluation:
        return cls(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            rule_id=str(row["rule_id"]),
            subject_type=row["subject_type"],
            subject_id=str(row["subject_id"]),
            result=row["result"],
            measured=dict(row["measured"]) if row.get("measured") else None,
            evaluated_at=row["evaluated_at"],
        )

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule_id": self.rule_id,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "result": self.result,
            "measured": self.measured,
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
        }
