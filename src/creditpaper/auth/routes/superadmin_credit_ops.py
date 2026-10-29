"""Read-only credit-operations endpoints for the superadmin console.

Every handler is a `GET`, enforces `require_superadmin`, and returns either a
stored-procedure payload or a download. `storage_path` never leaves this module -
it is stripped from document payloads before serialisation.
"""

import asyncio
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from creditpaper.auth.dependencies import CurrentUser, get_current_user
from creditpaper.auth.routes.superadmin import require_superadmin
from creditpaper.creditops import store
from creditpaper.deals.services.fact_sheet import (
    DealNotFound as FactSheetDealNotFound,
)
from creditpaper.deals.services.fact_sheet import (
    DealTypeRequired,
    fact_sheet_from_raw,
)
from creditpaper.deals.services.final_paper import (
    FinalPaperError,
    final_paper_actor_names,
    final_paper_logo_bytes,
    get_final_paper,
    render_final_docx,
)
from creditpaper.deals.services.workspace import DealNotFound, stream_version

router = APIRouter(prefix="/superadmin", tags=["superadmin-credit-ops"])

DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _strip_storage_paths(payload: dict) -> dict:
    """Remove filesystem locations from a documents payload before it is returned."""
    for document in payload.get("documents") or []:
        for version in document.get("versions") or []:
            version.pop("storage_path", None)
    return payload


def _deal_or_404(deal_id: str) -> str:
    """Resolve the deal's tenant for a download; 404 for unknown or deleted deals."""
    reference = store.get_sa_deal_ref(deal_id)
    if reference is None:
        raise HTTPException(status_code=404, detail="Deal not found.")
    return str(reference["tenant_id"])


@router.get("/credit-ops/overview")
async def credit_ops_overview(user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    require_superadmin(user)
    return await asyncio.to_thread(store.get_sa_overview)


@router.get("/deals/{deal_id}")
async def deal_reference(deal_id: str, user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    require_superadmin(user)
    reference = await asyncio.to_thread(store.get_sa_deal_ref, deal_id)
    if reference is None:
        raise HTTPException(status_code=404, detail="Deal not found.")
    return reference


@router.get("/deals/{deal_id}/workspace")
async def deal_workspace(deal_id: str, user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    require_superadmin(user)
    payload = await asyncio.to_thread(store.get_sa_deal_workspace, deal_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Deal not found.")
    return payload


@router.get("/deals/{deal_id}/documents")
async def deal_documents(deal_id: str, user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    require_superadmin(user)
    payload = await asyncio.to_thread(store.get_sa_deal_documents, deal_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Deal not found.")
    return _strip_storage_paths(payload)


@router.get("/deals/{deal_id}/facts")
async def deal_facts(deal_id: str, user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    require_superadmin(user)
    raw = await asyncio.to_thread(store.get_sa_fact_sheet, deal_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="Deal not found.")
    try:
        return fact_sheet_from_raw(deal_id, raw)
    except FactSheetDealNotFound:
        raise HTTPException(status_code=404, detail="Deal not found.") from None
    except DealTypeRequired:
        raise HTTPException(status_code=409, detail=DealTypeRequired.message) from None


@router.get("/deals/{deal_id}/validation")
async def deal_validation(deal_id: str, user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    require_superadmin(user)
    payload = await asyncio.to_thread(store.get_sa_validation, deal_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Deal not found.")
    return payload


@router.get("/deals/{deal_id}/paper")
async def deal_paper(deal_id: str, user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    require_superadmin(user)
    payload = await asyncio.to_thread(store.get_sa_paper, deal_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Deal not found.")
    return payload


@router.get("/deals/{deal_id}/documents/{document_id}/versions/{version_id}/download")
def download_document_version(
    deal_id: str,
    document_id: str,
    version_id: str,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
):
    require_superadmin(user)
    tenant_id = _deal_or_404(deal_id)
    try:
        data, filename, mime = stream_version(
            tenant_id=tenant_id,
            deal_id=deal_id,
            document_id=document_id,
            version_id=version_id,
        )
    except DealNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    safe_name = os.path.basename(filename or "").replace('"', "") or "document"
    return Response(
        data,
        media_type=mime or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.get("/deals/{deal_id}/paper/final/download")
def download_final_paper(deal_id: str, user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    """Re-render the approved paper, mirroring the tenant export exactly.

    The superadmin console downloads the same bytes a tenant would get: same
    renderer, same brand logo, same unresolved-actor resolution.
    """
    require_superadmin(user)
    tenant_id = _deal_or_404(deal_id)
    try:
        paper = get_final_paper(tenant_id, deal_id)
    except FinalPaperError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"error": {"code": exc.code, "message": str(exc)}},
        ) from None
    recommendation = paper.get("recommendation")
    approval_id = paper["approval"]["actor_id"]
    unresolved_ids = (
        [approval_id]
        if not paper["approval"].get("actor_name") or paper["approval"]["actor_name"] == "Former member"
        else []
    )
    if recommendation and (
        not recommendation.get("author_name") or recommendation["author_name"] == "Former member"
    ):
        unresolved_ids.append(recommendation["author_id"])
    names = final_paper_actor_names(tenant_id, unresolved_ids)
    if unresolved_ids:
        paper["approval"]["actor_name"] = (
            names.get(approval_id) or paper["approval"].get("actor_name") or "Former member"
        )
        if recommendation:
            recommendation["author_name"] = (
                names.get(recommendation["author_id"]) or recommendation.get("author_name") or "Former member"
            )
    logo_bytes = final_paper_logo_bytes(paper)
    return Response(
        render_final_docx(paper, logo_bytes=logo_bytes),
        media_type=DOCX_MEDIA,
        headers={"Content-Disposition": 'attachment; filename="approved-credit-paper.docx"'},
    )


@router.get("/maker-checker")
async def maker_checker_queue(
    tenant_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
):
    """Deals awaiting a decision, raised fact reviews, recent approvals.

    Observation only: this module exposes no mutation endpoint, so nothing here
    can approve, reject or re-run anything.
    """
    require_superadmin(user)
    if tenant_id:
        try:
            uuid.UUID(tenant_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid tenant id.") from None
    return await asyncio.to_thread(store.get_sa_queue, tenant_id or None, page, limit)
