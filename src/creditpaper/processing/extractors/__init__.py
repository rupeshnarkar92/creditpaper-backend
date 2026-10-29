"""Document extractors, dispatched by mime type.

Adding a new mime type: write `extract_<format>(raw, *, original_filename)`
in this package, then register it in EXTRACTORS_BY_MIME.
"""

from __future__ import annotations

from collections.abc import Callable

from creditpaper.processing.extractors.docx import extract_docx
from creditpaper.processing.extractors.image import extract_image
from creditpaper.processing.extractors.pdf import extract_pdf
from creditpaper.processing.extractors.xlsx import extract_xlsx
from creditpaper.processing.models import ExtractionResult

ExtractorFn = Callable[..., ExtractionResult]


EXTRACTORS_BY_MIME: dict[str, ExtractorFn] = {
    "application/pdf": extract_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_docx,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": extract_xlsx,
    "application/vnd.ms-excel": extract_xlsx,
    "text/csv": extract_xlsx,
    "image/png": extract_image,
    "image/jpeg": extract_image,
    "image/tiff": extract_image,
}


__all__ = [
    "EXTRACTORS_BY_MIME",
    "ExtractorFn",
    "extract_pdf",
    "extract_docx",
    "extract_xlsx",
    "extract_image",
]
