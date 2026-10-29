"""Template schemas — request/response models for the API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from creditpaper.templates.schemas.paper_type import PaperTypeOut


class SubsectionItem(BaseModel):
    """One subsection inside a section, numbered within it (1.1, 1.2, ...)."""

    number: str
    name: str = Field(..., min_length=1, max_length=120)


class SectionItem(BaseModel):
    """One section in the section order list, with its subsections in order."""

    number: str
    name: str
    subsections: list[SubsectionItem] = Field(default_factory=list, max_length=30)


class TemplateCreate(BaseModel):
    """Request body for creating a new custom template."""

    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    # Empty is allowed: a new or duplicated template is used by no deal until its deal
    # type is chosen (one template per deal type, enforced in template_service).
    paper_types: list[str] = Field(default_factory=list, max_length=1)


class TemplateUpdate(BaseModel):
    """Request body for partial update of a template."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    paper_types: list[str] | None = Field(default=None, max_length=20)
    sections: list[SectionItem] | None = None
    is_active: bool | None = None


class TemplateOut(BaseModel):
    """Response for a single template."""

    id: str
    name: str
    description: str | None
    paper_types: list[str]
    sections: list[dict[str, Any]]
    is_default: bool
    is_active: bool
    source: str
    created_at: str | None
    updated_at: str | None


class TemplatePaperOut(BaseModel):
    """Response for a template paper."""

    id: str
    filename: str
    is_credit_paper: bool
    approval_detected: bool
    confidence: float
    size: int
    content_type: str | None
    created_at: str | None


class TemplatesListResponse(BaseModel):
    """Response for GET /api/templates — default + custom."""

    default: TemplateOut | None
    custom: list[TemplateOut]
    paper_types: list[PaperTypeOut]


class SaveSectionsRequest(BaseModel):
    """Request body for saving section order."""

    sections: list[SectionItem] = Field(..., min_length=1, max_length=20)
