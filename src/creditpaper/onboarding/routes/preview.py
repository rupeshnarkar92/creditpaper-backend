from __future__ import annotations

from fastapi import APIRouter, Depends

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.onboarding.services.section_order import apply_saved_order
from creditpaper.onboarding.services.voice_service import get_section_preview
from creditpaper.onboarding.utils.store import load_section_order

router = APIRouter(prefix="/api/onboarding/preview", tags=["onboarding"])


@router.get("", response_model=dict)
def get_preview(
    tenant_id: str = Depends(resolve_tenant_id),
):
    """Sections in the firm's saved order; `default_sections` keeps the order the papers give."""
    preview = get_section_preview(tenant_id)
    merged = preview.get("sections") or []
    return {
        **preview,
        "sections": apply_saved_order(merged, load_section_order(tenant_id)),
        "default_sections": merged,
    }


@router.get("/sections", response_model=dict)
def get_sections_preview(
    tenant_id: str = Depends(resolve_tenant_id),
):
    return get_section_preview(tenant_id)
