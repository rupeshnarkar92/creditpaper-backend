"""Pipeline entry point. Called by FastAPI BackgroundTasks after upload.

Idempotent: re-running on a `ready` row is a no-op (returns immediately).
Failure modes are recorded in `processing_error`, not raised.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from creditpaper.ledger.jobs import processing_stage
from creditpaper.obs.timing import timed
from creditpaper.processing.chunker import chunk_text
from creditpaper.processing.classify import classify_document
from creditpaper.processing.extractors import EXTRACTORS_BY_MIME
from creditpaper.processing.models import ExtractionResult
from creditpaper.processing.store import (
    begin_processing,
    update_status,
    upsert_extraction,
)

log = logging.getLogger(__name__)


class OCRRequired(Exception):
    """The fast parser found evidence-bearing pages that require the OCR lane."""

    def __init__(self, pages: int):
        super().__init__(f"{pages} page(s) require OCR")
        self.pages = pages


def _strip_nul(value):
    """Remove PostgreSQL-incompatible NUL characters from extracted payloads."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_strip_nul(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_nul(item) for key, item in value.items()}
    return value


@timed("text_processing")
def process_version(
    version_id: str,
    *,
    tenant_id: str,
    deal_id: str,
    allow_ocr: bool = True,
    prepared: dict | None = None,
) -> bool:
    """Run text extraction on a version belonging to the queued tenant/deal.

    Returns True only when the version is ready (including an idempotent cache
    hit). Failure is also recorded on the version so the durable job cannot
    report success merely because this function returned normally.
    """
    row = prepared if prepared is not None else begin_processing(tenant_id, deal_id, version_id)

    if not row:
        log.warning("process_version: version not found for queued tenant/deal")
        return False
    if row["processing_status"] == "ready":
        return True  # idempotent

    storage_path = row["storage_path"]
    mime_type = row["mime_type"]
    document_id = row["document_id"]
    version_number = row["version_number"]
    original_filename = row["original_filename"]

    content_hash = row.get("content_sha256")
    if content_hash:
        cached = row.get("cached_extraction")
        if cached:
            upsert_extraction(
                version_id=version_id,
                tenant_id=tenant_id,
                deal_id=deal_id,
                document_id=str(document_id),
                version_number=version_number,
                mime_type=mime_type,
                full_text=cached["full_text"],
                page_texts=cached["page_texts"],
                chunks=cached["chunks"],
                page_count=cached.get("page_count"),
                sheet_count=cached.get("sheet_count"),
                char_count=cached["char_count"],
                classification=cached["classification"],
                finalize=True,
                content_hash=content_hash,
            )
            log.info("processing_cache_hit version=%s", version_id)
            return True

    path = Path(storage_path)
    stage_started = time.perf_counter()
    try:
        with processing_stage("read"):
            from creditpaper.security.document_storage import read_document

            raw = read_document(path, tenant_id=tenant_id, record_id=version_id)
        log.info(
            "processing_stage version=%s stage=read elapsed_s=%.3f",
            version_id,
            time.perf_counter() - stage_started,
        )
    except (OSError, ValueError) as e:
        log.error("process_version read failed: %s", type(e).__name__)
        update_status(
            version_id, "failed", reason=f"read_error:{str(e)[:400]}", tenant_id=tenant_id, deal_id=deal_id
        )
        return False

    extractor = EXTRACTORS_BY_MIME.get(mime_type)
    if not extractor:
        update_status(
            version_id,
            "failed",
            reason=f"unsupported_mime:{mime_type}",
            tenant_id=tenant_id,
            deal_id=deal_id,
        )
        return False
    if not allow_ocr and mime_type.startswith("image/"):
        raise OCRRequired(1)

    stage_started = time.perf_counter()
    try:
        with processing_stage("extract") as stage_details:
            extractor_options = {"original_filename": original_filename}
            if mime_type == "application/pdf":
                extractor_options["allow_ocr"] = allow_ocr
            result: ExtractionResult = extractor(raw, **extractor_options)
            ocr_attempted = sum(bool(page.get("ocr_attempted")) for page in result.page_texts)
            ocr_failed = sum(bool(page.get("ocr_failed")) for page in result.page_texts)
            ocr_deferred = sum(bool(page.get("ocr_deferred")) for page in result.page_texts)
            stage_details.update(
                pages=len(result.page_texts),
                ocr_attempted=ocr_attempted,
                ocr_failed=ocr_failed,
                ocr_deferred=ocr_deferred,
            )
        log.info(
            "processing_stage version=%s stage=extract elapsed_s=%.3f pages=%d "
            "ocr_attempted=%d ocr_failed=%d ocr_deferred=%d",
            version_id,
            time.perf_counter() - stage_started,
            len(result.page_texts),
            ocr_attempted,
            ocr_failed,
            ocr_deferred,
        )
    except Exception as e:
        log.error("PDF extraction failed: %s", type(e).__name__)
        update_status(
            version_id, "failed", reason=f"extract_error:{str(e)[:400]}", tenant_id=tenant_id, deal_id=deal_id
        )
        return False
    deferred_pages = sum(bool(page.get("ocr_deferred")) for page in result.page_texts)
    if deferred_pages and not allow_ocr:
        raise OCRRequired(deferred_pages)
    if not "".join(result.full_text.split()):
        log.warning("processing_unreadable version=%s", version_id)
        update_status(
            version_id,
            "failed",
            reason="unreadable_content:no_text_extracted",
            tenant_id=tenant_id,
            deal_id=deal_id,
        )
        return False
    # No vision at parse time (DECISIONS 2026-10-09): it read every scanned page,
    # ~$0.018 each. Vision now reads only the scanned pages a material or list fact
    # stands on, after extraction (`ingest/vision_check.py`).

    stage_started = time.perf_counter()
    with processing_stage("classify_chunk") as stage_details:
        classification = classify_document(
            result.full_text,
            original_filename,
            page_texts=result.page_texts,
        )
        chunks = chunk_text(result.full_text)
        # PDFs occasionally contain embedded NULs. PostgreSQL rejects them in
        # text/JSON parameters, so clean all extracted fields at the boundary.
        full_text = _strip_nul(result.full_text)
        page_texts = _strip_nul(result.page_texts)
        chunks = _strip_nul(chunks)
        classification = _strip_nul(classification)
        stage_details.update(chunks=len(chunks), characters=len(full_text))
    log.info(
        "processing_stage version=%s stage=classify_chunk elapsed_s=%.3f",
        version_id,
        time.perf_counter() - stage_started,
    )

    stage_started = time.perf_counter()
    try:
        with processing_stage("finalize", metadata={"cacheable": bool(content_hash)}):
            upsert_extraction(
                version_id=version_id,
                tenant_id=str(tenant_id),
                deal_id=deal_id,
                document_id=str(document_id),
                version_number=version_number,
                mime_type=mime_type,
                full_text=full_text,
                page_texts=page_texts,
                chunks=chunks,
                page_count=result.page_count,
                sheet_count=result.sheet_count,
                char_count=len(full_text),
                classification=classification,
                finalize=True,
                content_hash=content_hash,
            )
        log.info(
            "processing_stage version=%s stage=finalize elapsed_s=%.3f",
            version_id,
            time.perf_counter() - stage_started,
        )
    except Exception as e:
        log.error("Extraction persistence failed: %s", type(e).__name__)
        update_status(
            version_id, "failed", reason=f"db_write:{str(e)[:400]}", tenant_id=tenant_id, deal_id=deal_id
        )
        return False

    return True
