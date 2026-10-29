"""Render PDF pages to PNG bytes for vision-language models.

pypdfium2 renders (`ingest/reader.py`). The Anthropic provider does not call
this module: it reads PDF bytes natively. The Ollama provider routes PDFs
through here because qwen2.5-vl reads images, not base64 PDFs.
"""

from __future__ import annotations

import io


def render_pdf_pages(content: bytes, max_pages: int = 30) -> list[bytes]:
    """Encode each PDF page to PNG. Returns at most max_pages images."""
    if max_pages < 0:
        raise ValueError(f"max_pages must be non-negative, got {max_pages}")

    import pypdfium2

    from creditpaper.ingest.reader import PdfiumReader, render_pages

    try:
        with PdfiumReader(content) as reader:
            count = reader.page_count()
    except pypdfium2.PdfiumError as e:
        raise ValueError(f"pdf_render: could not open PDF content: {e}") from e

    pages: list[bytes] = []
    for image in render_pages(content, list(range(min(count, max_pages))), dpi=150):
        if image is None:
            continue
        out = io.BytesIO()
        image.save(out, format="PNG")
        pages.append(out.getvalue())
    return pages
