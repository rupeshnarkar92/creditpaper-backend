"""Shared readiness gate for facts and generation."""

from fastapi import HTTPException

from creditpaper.db import tenant_cursor


def status(tenant_id: str, deal_id: str, *, include_batches: bool = True) -> dict:
    with tenant_cursor(tenant_id) as cur:
        if include_batches:
            cur.execute(
                """SELECT public.deal_ingestion_status(%s::uuid,%s)
                   || jsonb_build_object(
                        'batches',public.document_archive_progress(%s::uuid,%s),
                        'accounting',public.deal_ingestion_accounting(%s::uuid,%s)
                      )""",
                (tenant_id, deal_id, tenant_id, deal_id, tenant_id, deal_id),
            )
        else:
            cur.execute("SELECT public.deal_ingestion_status(%s::uuid,%s)", (tenant_id, deal_id))
        return cur.fetchone()[0]


def require_ready(tenant_id: str, deal_id: str, *, capability: str = "generation") -> None:
    """Require enough processed evidence for a capability.

    Facts are progressively available after the first document is ready. Generation
    waits until every active upload/extraction is terminal so a paper is never built
    from a silently incomplete evidence set.
    """
    if capability not in {"facts", "generation"}:
        raise ValueError(f"unknown ingestion capability: {capability}")
    readiness = status(tenant_id, deal_id, include_batches=False)
    if readiness.get(f"{capability}_locked", readiness["locked"]):
        raise HTTPException(
            409,
            detail={
                "error": {
                    "code": "DOCUMENTS_NOT_READY",
                    "message": (
                        "No processed documents are available yet."
                        if capability == "facts"
                        else "Documents are still processing. Open Documents to check progress."
                    ),
                }
            },
        )
