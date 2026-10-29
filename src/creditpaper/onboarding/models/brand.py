"""Brand Pydantic models — request/response types for the brand analysis API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BrandAnalyzeRequest(BaseModel):
    website_url: str = Field(..., min_length=3, max_length=2048)


class BrandAnalyzeResponse(BaseModel):
    status: str
    website: str
    duration_ms: int
    brand: dict


class ManualBrandRequest(BaseModel):
    website_url: str | None = Field(default=None, max_length=2048)
    name: str | None = Field(default=None, max_length=80)
    colors: dict = Field(..., description="{primary, secondary, accent}")
