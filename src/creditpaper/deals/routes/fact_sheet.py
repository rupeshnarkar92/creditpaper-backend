"""Fact sheet route (contract 2026-09-15 §B). Mounted under `/api/deals` through the
workspace router.

  GET /api/deals/{deal_id}/fact-sheet

Read-only. Tenant-scoped: another tenant's deal answers 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.db import tenant_uuid
from creditpaper.deals.services import fact_sheet

router = APIRouter(tags=["fact-sheet"])


def _err(request: Request, code: str, message: str, status: int, field: str | None = None) -> HTTPException:
    body: dict[str, object] = {
        "code": code,
        "message": message,
        "request_id": request.headers.get("x-request-id", ""),
    }
    if field:
        body["field"] = field
    return HTTPException(status_code=status, detail={"error": body})


@router.get("/{deal_id}/fact-sheet")
def get_fact_sheet(deal_id: str, request: Request, tenant_id: str = Depends(resolve_tenant_id)):
    from creditpaper.deals.services.ingestion_gate import require_ready

    require_ready(tenant_id, deal_id, capability="facts")
    try:
        return fact_sheet.get_fact_sheet(str(tenant_uuid(tenant_id)), deal_id)
    except fact_sheet.DealNotFound:
        raise _err(request, "DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None
    except fact_sheet.DealTypeRequired:
        raise _err(
            request, "DEAL_TYPE_REQUIRED", fact_sheet.DealTypeRequired.message, 409, "deal_type"
        ) from None
