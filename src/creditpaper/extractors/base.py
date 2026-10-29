"""Extractor contract (Build Plan §4.3).

One extractor per document type; shared nothing but the deal-model schema
convention. Two-pass mandatory for long documents. Anchors at parse time (D4).
Concrete extractors land in Wave 1, each with a fixture set and binary
acceptance criteria.

Extractors never write to a DealModel: output is applied through
model/apply.py, the single write path (DECISIONS.md 2026-06-11). Paths must
satisfy the citation grammar (model/citations.py); collection keys are made
with `slugify`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from creditpaper.model.apply import ExtractionOutput

__all__ = ["ExtractionOutput", "Extractor"]


class Extractor(ABC):
    """Subclasses set `slot_key` and implement extract()."""

    slot_key: str

    @abstractmethod
    def extract(
        self,
        document_id: str,
        path: Path,
        *,
        page_texts: list[dict] | None = None,
    ) -> ExtractionOutput:
        """Parse one document. Every emitted field MUST carry provenance (D4).

        Args:
            document_id: Unique identifier for this document (used in provenance).
            path: Path to the document file on disk.
            page_texts: Optional S2 page-text dicts ``{"page": int, "text": str}``.
                        When provided, the extractor SHOULD use the page selector
                        to pick relevant pages and render them as PNGs instead of
                        sending the full document. When None, fall back to the
                        legacy full-document b64 path.
        """

    def structural_pass(self, path: Path) -> dict:
        """Optional pass 1 for long documents: locate where things live."""
        raise NotImplementedError
