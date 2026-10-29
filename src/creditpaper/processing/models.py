from __future__ import annotations

from pydantic import BaseModel, Field


class PageText(BaseModel):
    """One page (PDF/image) or sheet (XLSX) of extracted text."""

    page: int
    text: str
    ocr_used: bool = False
    ocr_attempted: bool = False
    ocr_failed: bool = False
    ocr_deferred: bool = False
    sheet_name: str | None = None


class ExtractionResult(BaseModel):
    """Output of one extractor. Chunker and classifier consume this."""

    full_text: str
    page_texts: list[dict] = Field(default_factory=list)
    page_count: int | None = None
    sheet_count: int | None = None
