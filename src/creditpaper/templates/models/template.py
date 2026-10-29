"""Dataclass-style row models for templates.

Mirrors the pattern in onboarding/utils/store.py — the actual DB queries
are in store/template_store.py; these are the typed representations of rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class Template:
    """One row of the `templates` table."""

    id: str
    tenant_id: str
    name: str
    description: str | None
    paper_types: list[str]  # array of paper_type ids, e.g. ["bridge", "construction"]
    sections: list[dict[str, Any]]  # [{"number": "01", "name": "..."}, ...]
    is_default: bool
    is_active: bool
    source: str  # "onboarding" | "custom"
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Template:
        return cls(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            name=row["name"],
            description=row.get("description"),
            paper_types=list(row.get("paper_types") or []),
            sections=list(row.get("sections") or []),
            is_default=bool(row.get("is_default", False)),
            is_active=bool(row.get("is_active", True)),
            source=row.get("source", "custom"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "paper_types": self.paper_types,
            "sections": self.sections,
            "is_default": self.is_default,
            "is_active": self.is_active,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class TemplatePaper:
    """One row of the `template_papers` table."""

    id: str
    template_id: str
    tenant_id: str
    filename: str
    storage_path: str
    is_credit_paper: bool
    approval_detected: bool
    confidence: float
    size: int
    content_type: str | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> TemplatePaper:
        return cls(
            id=str(row["id"]),
            template_id=str(row["template_id"]),
            tenant_id=str(row["tenant_id"]),
            filename=row["filename"],
            storage_path=row["storage_path"],
            is_credit_paper=bool(row.get("is_credit_paper", False)),
            approval_detected=bool(row.get("approval_detected", False)),
            confidence=float(row.get("confidence", 0.0)),
            size=int(row.get("size", 0)),
            content_type=row.get("content_type"),
            created_at=row["created_at"],
        )

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "is_credit_paper": self.is_credit_paper,
            "approval_detected": self.approval_detected,
            "confidence": self.confidence,
            "size": self.size,
            "content_type": self.content_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
