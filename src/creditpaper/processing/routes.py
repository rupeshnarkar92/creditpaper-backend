"""HTTP routes for deal document extraction.

Single endpoint: GET one version's extraction. 404 if the version is
unknown or the extraction has not finished yet (frontend polls).
410 if the extraction failed (body includes the error reason).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.db import tenant_cursor, tenant_uuid

router = APIRouter(prefix="/api/deals", tags=["deals-extraction"])


@router.get(
    "/{deal_id}/documents/{document_id}/versions/{version_id}/extraction",
)
def get_extraction(
    deal_id: str,
    document_id: str,
    version_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
):
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """
            SELECT v.processing_status, v.processing_error,
                   e.full_text, e.page_texts, e.chunks,
                   e.page_count, e.sheet_count, e.char_count,
                   e.classification, e.processed_at
            FROM deal_document_versions v
            LEFT JOIN deal_document_extractions e ON e.version_id = v.id
            WHERE v.id = %s AND v.document_id = %s AND v.deal_id = %s AND v.tenant_id = %s
            """,
            (version_id, document_id, deal_id, tid),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "DEAL_DOCUMENT_EXTRACTION_NOT_READY",
                    "message": f"version {version_id} not found",
                }
            },
        )

    (
        status,
        error,
        full_text,
        page_texts,
        chunks,
        page_count,
        sheet_count,
        char_count,
        classification,
        processed_at,
    ) = row

    if status in ("pending", "processing"):
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "DEAL_DOCUMENT_EXTRACTION_NOT_READY",
                    "message": f"version {version_id} extraction is {status}",
                }
            },
        )

    if status == "failed":
        raise HTTPException(
            status_code=410,
            detail={
                "error": {"code": "DEAL_DOCUMENT_EXTRACTION_FAILED", "message": error or "extraction failed"}
            },
        )

    return {
        "version_id": version_id,
        "processing_status": status,
        "full_text": full_text,
        "page_texts": page_texts,
        "chunks": chunks,
        "page_count": page_count,
        "sheet_count": sheet_count,
        "char_count": char_count,
        "classification": classification,
        "processed_at": processed_at.isoformat() if processed_at else None,
    }
