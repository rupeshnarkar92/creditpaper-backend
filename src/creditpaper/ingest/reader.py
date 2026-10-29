"""Page access behind a protocol, so the pipeline is not welded to one library.

pypdfium2 is the current engine (Apache-licensed, not AGPL, unlike pymupdf).
Only this module imports it.

``PdfiumReader.page`` reads with ``textpage.get_text_range()``, never
``get_text_bounded()``: on tables, bounded rendering fuses adjacent cells with
no separator, inventing figures ("Land 1,000,0000" out of "Land 1,000,000" next
to "0"). ``get_text_range()``'s character count matches ``count_chars()`` - one
character per glyph read, nothing fused.

pdfium marks a soft line-end hyphen with U+FFFE (seen from ``get_text_range``)
or ``"\\x02"`` (seen from ``get_text_bounded``, which this reader does not call,
but the marker is translated too in case another caller ever routes through
it). Both are mapped to ``"-\\n"``, the shape ``text_norm.normalise`` already
rejoins a hyphenated word from ("mill\\ufffeion" -> "mill-\\nion" -> "million").

libpdfium is not thread-safe: calling into it from two threads at once (e.g. a
thread pool extracting page text from several documents concurrently) has been
observed to silently corrupt reads on some pages (text comes back empty on a
page that has real text) and, once triggered, to poison pdfium's process-wide
state so that every later call in the process raises ``PdfiumError`` -- even
single-threaded, unrelated ones. ``_PDFIUM_LOCK`` serialises every call this
reader makes into pdfium so no two threads are ever inside it at once. Page
and textpage handles are also closed explicitly rather than left to the
garbage collector -- pdfium's own consistency check (closing a document with
unclosed children) warns about exactly this ("kids weakrefs have not been
cleaned up"), and leaving it to GC timing is one more way for a handle to
outlive its parent under load.

Text drawn by annotations is not part of the page's content stream, so
``get_textpage()`` never sees it: a FreeText annotation (a typed-on note, e.g.
figures added to a marked-up plan) or a filled AcroForm field (the widget's
appearance stream holds the typed value) reads as nothing. Measured on the
fixtures: 8 pages whose only text is FreeText came back empty, and 55 more
lost their form-field values. ``page`` therefore flattens a page that carries
annotations (``FPDFPage_Flatten``, ``FLAT_NORMALDISPLAY``: only what displays
on screen, hidden annotations stay out) and reloads it before reading, which
merges those appearance streams into the page content. Flattening only touches
the in-memory document; nothing is saved. It does not need ``init_forms``:
it reads the appearance streams already in the file. A filled field whose
value overflows its box is read in full, because pdfium's text page does not
clip -- the clipped tail is part of the field's value, not invented text.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Protocol

import pypdfium2
from pydantic import BaseModel

if TYPE_CHECKING:
    from PIL import Image

_HYPHEN_MARKERS = {0xFFFE: "-\n", 0x02: "-\n"}
_PDFIUM_LOCK = threading.Lock()
# What ``PdfiumReader.page`` returns, as a version: part of every page-text cache
# key, so text read by an older reader is never served as this one's. Bump it on
# ANY change to what ``page`` returns; tests/ingest/test_reader.py pins it to the
# source of ``page``, so a change without a bump fails. History: 1 original,
# 2 pdfium lock (d9cc55d), 3 annotation flattening (64dfff5).
READER_VERSION = "pdfium-4"


class PageContent(BaseModel):
    number: int
    text: str
    has_image: bool
    word_count: int
    has_visual_content: bool = False


class PageReader(Protocol):
    def page_count(self) -> int: ...
    def page(self, number: int) -> PageContent: ...


class PdfiumReader:
    """Reads pages of the PDF at ``path`` through pypdfium2.

    A context manager, and also closeable directly with ``close()``. ``page``
    validates its argument and raises ``IndexError`` outside ``1..page_count``.
    """

    def __init__(self, source: str | Path | bytes) -> None:
        with _PDFIUM_LOCK:
            self._doc: pypdfium2.PdfDocument | None = pypdfium2.PdfDocument(
                source if isinstance(source, bytes) else str(source)
            )

    def __enter__(self) -> PdfiumReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._doc is not None:
            with _PDFIUM_LOCK:
                self._doc.close()
            self._doc = None

    def _require_doc(self) -> pypdfium2.PdfDocument:
        if self._doc is None:
            raise RuntimeError("PdfiumReader is closed")
        return self._doc

    def page_count(self) -> int:
        with _PDFIUM_LOCK:
            return len(self._require_doc())

    def page(self, number: int) -> PageContent:
        with _PDFIUM_LOCK:
            doc = self._require_doc()
            count = len(doc)
            if not 1 <= number <= count:
                raise IndexError(f"page {number} out of range for a {count}-page document")
            pdfium_page = doc[number - 1]
            if pypdfium2.raw.FPDFPage_GetAnnotCount(pdfium_page.raw) > 0:
                flattened = pypdfium2.raw.FPDFPage_Flatten(pdfium_page.raw, pypdfium2.raw.FLAT_NORMALDISPLAY)
                if flattened == pypdfium2.raw.FLATTEN_SUCCESS:
                    # Flattening rewrites the page dictionary; reload so the
                    # text page is built from the merged content.
                    pdfium_page.close()
                    pdfium_page = doc[number - 1]
            textpage = pdfium_page.get_textpage()
            try:
                text = textpage.get_text_range().translate(_HYPHEN_MARKERS)
                has_image = False
                has_visual_content = False
                # Some construction drawings contain hundreds of thousands of
                # vector objects. We only need two booleans, so never materialise
                # or fully walk that collection once an image is found.
                for obj in pdfium_page.get_objects():
                    has_visual_content = True
                    if obj.type == pypdfium2.raw.FPDF_PAGEOBJ_IMAGE:
                        has_image = True
                        break
            finally:
                textpage.close()
                pdfium_page.close()
        return PageContent(
            number=number,
            text=text,
            has_image=has_image,
            has_visual_content=has_visual_content,
            word_count=len(text.split()),
        )


def render_pages(source: str | Path | bytes, indices: Sequence[int], *, dpi: int) -> list[Image.Image | None]:
    """Render 0-based pages of a PDF to RGB images at ``dpi``; None for a page that failed or does not exist.

    Under ``_PDFIUM_LOCK`` like every other call into pdfium. Annotations and form
    fields are drawn, as a viewer shows the page, which is what OCR and vision read.
    """
    out: list[Image.Image | None] = []
    with _PDFIUM_LOCK:
        doc = pypdfium2.PdfDocument(source if isinstance(source, bytes) else str(source))
        try:
            count = len(doc)
            for index in indices:
                if not 0 <= index < count:
                    out.append(None)
                    continue
                page = doc[index]
                try:
                    bitmap = page.render(scale=dpi / 72, may_draw_forms=True)
                    try:
                        out.append(bitmap.to_pil().convert("RGB"))
                    finally:
                        bitmap.close()
                except Exception:  # one unrenderable page must not lose the rest
                    out.append(None)
                finally:
                    page.close()
        finally:
            doc.close()
    return out


def read_toc(source: str | Path | bytes) -> list[tuple[int, str, int]]:
    """The PDF's outline as ``(level, title, page)`` rows: level and page 1-based, document order.

    The shape PyMuPDF's ``get_toc`` returned, so callers kept their logic. A row
    with no destination page gets page 1."""
    rows: list[tuple[int, str, int]] = []
    with _PDFIUM_LOCK:
        doc = pypdfium2.PdfDocument(source if isinstance(source, bytes) else str(source))
        try:
            for bookmark in doc.get_toc():
                dest = bookmark.get_dest()
                index = dest.get_index() if dest is not None else None
                rows.append((bookmark.level + 1, bookmark.get_title() or "", (index or 0) + 1))
        finally:
            doc.close()
    return rows
