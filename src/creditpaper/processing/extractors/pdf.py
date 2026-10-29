"""Local PDF extraction with original page anchors and Tesseract OCR.

Text through pypdfium2 (`ingest/reader.py`): `get_text_range`, which never fuses
adjacent table cells, with annotations and form fields flattened in so typed-on
values are read. PyMuPDF was replaced on 2026-10-07 (AGPL; DECISIONS).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import cache
from multiprocessing import get_context
from weakref import WeakSet

from creditpaper.ingest.reader import PdfiumReader, render_pages
from creditpaper.obs.timing import timed
from creditpaper.processing.models import ExtractionResult, PageText
from creditpaper.processing.ocr import ocr_pages

log = logging.getLogger(__name__)

_OCR_THRESHOLD_CHARS = 50

# Render resolution for the OCR fallback. Measured 2026-09-25 on 12 real scanned
# pages, with OMP_THREAD_LIMIT=1:
#
#     200 dpi   736 ms/page   18,927 characters   (the default)
#     150 dpi   693 ms/page   18,839 characters   -0.5% text, -6% time
#     120 dpi   551 ms/page   18,684 characters   -1.3% text, -25% time
#
# 120 is the production default after the 178-document corpus run: it retained
# 98.7% of the measured characters while the complete parse finished inside
# the four-minute SLA. OCR evidence remains machine-read until independently
# confirmed, so the small resolution trade cannot silently verify a fact.
_DPI = int(os.getenv("OCR_DPI", "120"))

# How many pages of ONE document are rendered before any of them is read.
#
# Rendering goes through pdfium under its process-wide lock, so the batch is what
# keeps the shared OCR pool fed. It is also what
# bounds memory: a 200 DPI RGB page is about 11 MB raw, and with eight
# documents parsing at once the peak is `documents x OCR_BATCH x 11 MB`.
#
# Measured 2026-09-25 over 40 real documents (298 OCR'd pages), against the
# same parse read one page at a time:
#
#     one page at a time      111.0s
#     OCR_BATCH=4              81.2s
#     OCR_BATCH=8              77.8s
#
# 4 is the default rather than 8: the extra 4% is inside the noise of a single
# pair of runs and it doubles peak memory to roughly 700 MB. `OCR_BATCH` raises
# it for a deployment with memory to spare.
_BATCH = int(os.getenv("OCR_BATCH", "4"))
_MAX_OCR_PAGES = max(1, int(os.getenv("OCR_MAX_PAGES_PER_DOCUMENT", "100")))
_POOL_RESET_LOCK = threading.Lock()
_RESET_POOLS: WeakSet = WeakSet()


def _needs_ocr(text: str, *, has_image: bool, has_visual_content: bool) -> bool:
    """Select evidence-bearing pages, excluding blank and short born-digital pages."""
    if not text:
        return has_visual_content
    return len(text) < _OCR_THRESHOLD_CHARS and has_image


def _extract_pages(raw: bytes, *, allow_ocr: bool = True) -> tuple[str, list[PageText]]:
    """Extract text per page, reading any page with no text layer via OCR."""
    texts: list[str] = []
    has_images: list[bool] = []
    has_visual_content: list[bool] = []
    with PdfiumReader(raw) as reader:
        for n in range(1, reader.page_count() + 1):
            try:
                page = reader.page(n)
                texts.append(page.text.strip())
                has_images.append(page.has_image)
                has_visual_content.append(page.has_visual_content)
            except Exception:
                texts.append("")
                # A failed structural read is conservatively rendered: unlike
                # a known blank page, we do not know whether evidence exists.
                has_images.append(False)
                has_visual_content.append(True)

    # Whole batches go to the shared pool, so the scanned pages of one document
    # are read across cores instead of one after another.
    eligible = [
        i
        for i, text in enumerate(texts)
        if _needs_ocr(text, has_image=has_images[i], has_visual_content=has_visual_content[i])
    ]
    scanned = eligible[:_MAX_OCR_PAGES] if allow_ocr else []
    deferred_indices = set(eligible[_MAX_OCR_PAGES:] if allow_ocr else eligible)
    read_by_ocr = [False] * len(texts)
    ocr_attempted = [False] * len(texts)
    ocr_failed = [False] * len(texts)
    for start in range(0, len(scanned), max(1, _BATCH)):
        batch = scanned[start : start + max(1, _BATCH)]
        for i in batch:
            ocr_attempted[i] = True
        rendered = render_pages(raw, batch, dpi=_DPI)
        pairs = [(i, img) for i, img in zip(batch, rendered, strict=True) if img is not None]
        if len(pairs) < len(batch):
            log.warning("Local PDF page render failed on %d page(s)", len(batch) - len(pairs))
        rendered_indices = {i for i, _img in pairs}
        for i in batch:
            if i not in rendered_indices:
                ocr_failed[i] = True
        for (i, _img), read in zip(pairs, ocr_pages([img for _i, img in pairs]), strict=True):
            try:
                if read is None:
                    ocr_failed[i] = True
                    continue
                texts[i] = read.strip()
                read_by_ocr[i] = True
            finally:
                _img.close()

    pages = [
        PageText(
            page=n,
            text=text,
            ocr_used=used,
            ocr_attempted=attempted,
            ocr_failed=failed,
            ocr_deferred=(n - 1) in deferred_indices,
        )
        for n, (text, used, attempted, failed) in enumerate(
            zip(texts, read_by_ocr, ocr_attempted, ocr_failed, strict=True), start=1
        )
    ]
    full_text = "\n\n".join(p.text for p in pages)
    return full_text, pages


def _extract_pdf_local(raw: bytes, original_filename: str, *, allow_ocr: bool = True) -> ExtractionResult:
    # Page-level extraction is authoritative: provenance requires original page numbers.
    full_text, pages = _extract_pages(raw, allow_ocr=allow_ocr)
    return ExtractionResult(
        full_text=full_text,
        page_texts=[p.model_dump() for p in pages],
        page_count=len(pages),
    )


def _process_extract(raw: bytes, original_filename: str, allow_ocr: bool) -> dict:
    """Pickle-safe process entry point; the child never submits another process."""
    return _extract_pdf_local(raw, original_filename, allow_ocr=allow_ocr).model_dump()


def _init_pdf_worker() -> None:
    # Each PDF process owns a small OCR thread pool. Across processes the total
    # remains bounded by PDF_EXTRACT_PROCESSES * PDF_OCR_WORKERS.
    os.environ["OCR_WORKERS"] = os.getenv("PDF_OCR_WORKERS", "3")


@cache
def _pdf_pool() -> ProcessPoolExecutor:
    workers = max(1, int(os.getenv("PDF_EXTRACT_PROCESSES", "4")))
    return ProcessPoolExecutor(
        max_workers=workers,
        mp_context=get_context("spawn"),
        initializer=_init_pdf_worker,
    )


def _terminate_pdf_pool(pool: ProcessPoolExecutor) -> None:
    """Kill wedged parser children and force the next request onto a fresh pool."""
    with _POOL_RESET_LOCK:
        if pool in _RESET_POOLS:
            return
        _RESET_POOLS.add(pool)
        _pdf_pool.cache_clear()
        terminate = getattr(pool, "terminate_workers", None)
        if terminate is not None:
            terminate()
        else:  # Python <3.14 fallback; running children exit with their process.
            pool.shutdown(wait=False, cancel_futures=True)


@timed("pdf_parsing")
def extract_pdf(raw: bytes, *, original_filename: str, allow_ocr: bool = True) -> ExtractionResult:
    """Extract text and page anchors locally; optionally isolate PDFium per process."""
    processes = int(os.getenv("PDF_EXTRACT_PROCESSES", "4"))
    if processes <= 0:
        return _extract_pdf_local(raw, original_filename, allow_ocr=allow_ocr)
    timeout = max(1.0, float(os.getenv("PDF_EXTRACT_TIMEOUT_S", "120")))
    deadline = time.monotonic() + timeout
    for attempt in range(2):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"PDF extraction exceeded {timeout:g} seconds")
        with _POOL_RESET_LOCK:
            pool = _pdf_pool()
        future = None
        try:
            future = pool.submit(_process_extract, raw, original_filename, allow_ocr)
            payload = future.result(timeout=max(0, deadline - time.monotonic()))
        except TimeoutError as exc:
            if future is not None:
                future.cancel()
            _terminate_pdf_pool(pool)
            raise TimeoutError(f"PDF extraction exceeded {timeout:g} seconds") from exc
        except BrokenProcessPool:
            _terminate_pdf_pool(pool)
            if attempt:
                raise
            # A different document may have timed out and killed our shared
            # child. Retry once on the replacement within the original deadline.
        else:
            return ExtractionResult.model_validate(payload)
    raise RuntimeError("PDF process recovery exhausted")
