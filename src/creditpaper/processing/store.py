"""Persistence layer for the processing pipeline.

`update_status` writes to `deal_document_versions.processing_status`.
`upsert_extraction` is idempotent on `version_id` (UNIQUE constraint).
`maybe_update_document_kind` writes the parent document's
`document_kind` only when the slot is currently NULL (first-write wins;
user edits in the UI are preserved).
"""

from __future__ import annotations

import json
import os
from typing import Any

from creditpaper.db import tenant_cursor
from creditpaper.ingest.reader import READER_VERSION

_VALID_STATUSES = {"pending", "processing", "ready", "failed"}
# The shared extraction cache must move whenever the PDF reader moves. Keeping
# this as a static v1 previously served old OCR/page flags after reader changes.
PARSER_VERSION = f"extract-v2-{READER_VERSION}"
if cache_namespace := os.getenv("PROCESSING_CACHE_NAMESPACE", "").strip():
    # Benchmark/rollout namespace: isolates cache populations without deleting
    # durable production cache rows. Unset preserves the historical key.
    PARSER_VERSION = f"{PARSER_VERSION}-{cache_namespace}"


def content_sha256(tenant_id: str, deal_id: str, version_id: str) -> str | None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT public.document_content_sha256(%s,%s,%s)", (tenant_id, deal_id, version_id))
        return cur.fetchone()[0]


def cached_extraction(tenant_id: str, content_hash: str) -> dict | None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.get_processing_extraction_cache(%s,%s,%s)",
            (tenant_id, content_hash, PARSER_VERSION),
        )
        return cur.fetchone()[0]


def cache_extraction(tenant_id: str, content_hash: str, payload: dict) -> None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.put_processing_extraction_cache(%s,%s,%s,%s::jsonb)",
            (tenant_id, content_hash, PARSER_VERSION, json.dumps(payload)),
        )


def begin_processing(tenant_id: str, deal_id: str, version_id: str) -> dict | None:
    """Load metadata/cache and mark processing in one fenced transaction."""
    from creditpaper.ledger.jobs import fence

    with tenant_cursor(tenant_id) as cur:
        fence(cur)
        cur.execute(
            "SELECT public.begin_document_processing_cached(%s,%s,%s,%s)",
            (tenant_id, deal_id, version_id, PARSER_VERSION),
        )
        return cur.fetchone()[0]


def update_status(
    version_id: str,
    status: str,
    reason: str | None = None,
    *,
    tenant_id: str,
    deal_id: str,
) -> None:
    """Update only a version belonging to the queued tenant/deal."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"invalid processing_status: {status!r}")
    from creditpaper.db import tenant_uuid

    # Exception text from extractors can carry NULs; PostgreSQL rejects them.
    if reason is not None:
        reason = reason.replace("\x00", "")

    with tenant_cursor(tenant_id) as cur:
        from creditpaper.ledger.jobs import fence

        fence(cur)
        cur.execute(
            "SELECT public.set_document_processing_status(%s, %s, %s, %s, %s)",
            (tenant_uuid(tenant_id), deal_id, version_id, status, reason),
        )
        if not cur.fetchone()[0]:
            from creditpaper.ledger.types import NotFound

            raise NotFound("Document version not found")


def defer_to_ocr(tenant_id: str, deal_id: str, version_id: str, payload: dict) -> str:
    """Atomically return a version to pending and enqueue its OCR job."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.defer_document_to_ocr(%s,%s,%s,%s::jsonb)",
            (tenant_id, deal_id, version_id, json.dumps(payload)),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        raise RuntimeError("OCR job could not be queued")
    return str(row[0])


def upsert_extraction(
    *,
    version_id: str,
    tenant_id: str,
    deal_id: str,
    document_id: str,
    version_number: int,
    mime_type: str,
    full_text: str,
    page_texts: list[dict],
    chunks: list[dict],
    page_count: int | None,
    sheet_count: int | None,
    char_count: int,
    classification: dict,
    finalize: bool = False,
    content_hash: str | None = None,
) -> None:
    if finalize:
        from creditpaper.ledger.retrieval import page_index_chunks

        payload = {
            "full_text": full_text,
            "page_texts": page_texts,
            "chunks": chunks,
            "page_count": page_count,
            "sheet_count": sheet_count,
            "char_count": char_count,
            "classification": classification,
            "index_chunks": page_index_chunks(page_texts),
        }
        from creditpaper.ledger.jobs import current_lease, fence

        lease = current_lease()
        if lease is None:
            with tenant_cursor(tenant_id) as cur:
                fence(cur)
                cur.execute(
                    """SELECT public.finish_document_processing_cached(
                        %s,%s,%s,%s,%s,%s::jsonb)""",
                    (
                        tenant_id,
                        deal_id,
                        version_id,
                        content_hash,
                        PARSER_VERSION,
                        json.dumps(payload),
                    ),
                )
                if not cur.fetchone()[0]:
                    from creditpaper.ledger.types import NotFound

                    raise NotFound("Document version not found")
        else:
            from creditpaper.processing.finalize_batch import finalize_document

            finalize_document(
                tenant_id,
                {
                    "job_id": lease[1],
                    "lease": lease[2],
                    "deal_id": deal_id,
                    "version_id": version_id,
                    "content_hash": content_hash,
                    "parser_version": PARSER_VERSION,
                    "payload": payload,
                },
            )
        return
    with tenant_cursor(tenant_id) as cur:
        from creditpaper.ledger.jobs import fence

        fence(cur)
        cur.execute(
            "SELECT public.upsert_document_extraction(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                tenant_id,
                deal_id,
                document_id,
                version_id,
                version_number,
                mime_type,
                full_text,
                json.dumps(page_texts),
                json.dumps(chunks),
                page_count,
                sheet_count,
                char_count,
                json.dumps(classification),
            ),
        )
        from creditpaper.ledger.retrieval import index_pages

        index_pages(cur, tenant_id, deal_id, version_id, page_texts)


def maybe_update_document_kind(
    document_id: str,
    tenant_id: str,
    deal_id: str,
    classification: dict[str, Any],
) -> None:
    """Set document_kind on the parent row if currently NULL.

    User edits in the UI win unconditionally — a non-NULL current_kind
    is left alone, regardless of classifier confidence. S2 only fills
    the slot the first time.
    """
    new_kind = classification.get("kind")
    if not new_kind:
        return
    from creditpaper.db import tenant_uuid

    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.set_document_kind_if_unset(%s, %s, %s, %s)",
            (tenant_uuid(tenant_id), deal_id, document_id, new_kind),
        )
