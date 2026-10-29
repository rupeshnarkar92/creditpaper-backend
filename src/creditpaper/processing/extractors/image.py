"""Direct OCR for image uploads (PNG/JPEG/TIFF)."""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

from creditpaper.processing.limits import MAX_RASTER_PIXELS, DocumentResourceLimitExceeded
from creditpaper.processing.models import ExtractionResult
from creditpaper.processing.ocr import ocr_page


def extract_image(raw: bytes, *, original_filename: str) -> ExtractionResult:
    with Image.open(io.BytesIO(raw)) as img:
        if img.width * img.height > MAX_RASTER_PIXELS:
            raise DocumentResourceLimitExceeded(f"Image has more than {MAX_RASTER_PIXELS} pixels")
        img.verify()
    try:
        with Image.open(io.BytesIO(raw)) as img:
            text = ocr_page(img).strip()
    except UnidentifiedImageError as exc:
        raise ValueError("Unsupported or invalid image") from exc
    return ExtractionResult(
        full_text=text,
        page_texts=[{"page": 1, "text": text, "ocr_used": True}],
        page_count=1,
    )
