"""Immutable approved paper reads and exports."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.deals.services.final_paper import (
    FinalPaperError,
    final_paper_actor_names,
    final_paper_logo_bytes,
    get_draft_paper,
    get_final_paper,
    public_final_paper,
    render_final_docx,
)

router = APIRouter(prefix="/api/deals", tags=["final-paper"])


def _read(tenant_id: str, deal_id: str):
    try:
        return public_final_paper(get_final_paper(tenant_id, deal_id))
    except FinalPaperError as exc:
        raise HTTPException(exc.status, detail={"error": {"code": exc.code, "message": str(exc)}}) from None


@router.get("/{deal_id}/paper/draft/download")
def draft_paper_download(deal_id: str, tenant_id: str = Depends(resolve_tenant_id)):
    paper = get_draft_paper(tenant_id, deal_id)
    logo_bytes = final_paper_logo_bytes({**paper, "_export_brand_profile": None})
    return Response(
        render_final_docx(paper, logo_bytes=logo_bytes, draft=True),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="draft-credit-paper.docx"'},
    )


@router.get("/{deal_id}/paper/final")
def final_paper(deal_id: str, tenant_id: str = Depends(resolve_tenant_id)):
    return _read(tenant_id, deal_id)


@router.get("/{deal_id}/paper/final/download")
def final_paper_download(deal_id: str, tenant_id: str = Depends(resolve_tenant_id)):
    paper = get_final_paper(tenant_id, deal_id)
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
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="approved-credit-paper.docx"'},
    )
