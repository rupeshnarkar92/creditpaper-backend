"""Resource limits for parsing untrusted deal documents."""

from __future__ import annotations

MAX_PDF_PAGES = 400
MAX_RASTER_PIXELS = 40_000_000


class DocumentResourceLimitExceeded(ValueError):
    """Raised when parsing would exceed the configured per-document resource budget."""
