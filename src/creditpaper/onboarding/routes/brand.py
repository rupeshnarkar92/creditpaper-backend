from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import CurrentUser, require_permission
from creditpaper.onboarding.models.brand import (
    BrandAnalyzeRequest,
    BrandAnalyzeResponse,
    ManualBrandRequest,
)
from creditpaper.onboarding.services.brand_service import analyze_website
from creditpaper.onboarding.services.manual_brand import manual_brand_profile
from creditpaper.onboarding.services.website_fetcher import WebsiteFetchError
from creditpaper.onboarding.utils.store import load_brand_profile, save_brand_profile
from creditpaper.paper.theme import default_theme_colour, default_theme_fonts

router = APIRouter(prefix="/api/onboarding/brand", tags=["onboarding"])


@router.post("/analyze", response_model=BrandAnalyzeResponse)
async def post_analyze(
    body: BrandAnalyzeRequest,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _admin: CurrentUser = Depends(require_permission("settings.update")),  # noqa: B008
):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    try:
        result = await analyze_website(tenant_id, body.website_url)
        return result
    except WebsiteFetchError as e:
        status_map = {
            "INVALID_WEBSITE_URL": 400,
            "WEBSITE_UNREACHABLE": 502,
            "WEBSITE_TIMEOUT": 504,
            "WEBSITE_BLOCKED": 403,
            "WEBSITE_SCRAPING_NOT_ALLOWED": 403,
            "WEBSITE_CONTENT_UNSUPPORTED": 422,
            "BRAND_ANALYSIS_FAILED": 502,
        }
        friendly = {
            "INVALID_WEBSITE_URL": "Please enter a valid HTTPS website URL.",
            "WEBSITE_UNREACHABLE": "We couldn't reach this website. Check the URL and try again.",
            "WEBSITE_TIMEOUT": "This website took too long to respond. Please try again.",
            "WEBSITE_BLOCKED": "This website address is not allowed for security reasons.",
            "WEBSITE_SCRAPING_NOT_ALLOWED": "This website does not permit automated scraping. Please enter your brand details manually or try another site.",  # noqa: E501
            "WEBSITE_CONTENT_UNSUPPORTED": str(e)  # noqa: E501
            if "too large" in str(e).lower() or "too little" in str(e).lower()
            else "This website returned an unsupported format.",  # noqa: E501
            "BRAND_ANALYSIS_FAILED": "We couldn't analyze this website. Please try again or adjust colors manually.",  # noqa: E501
        }
        code = e.code if hasattr(e, "code") else "BRAND_ANALYSIS_FAILED"
        http_status = status_map.get(code, 400)
        message = friendly.get(code, str(e))
        raise HTTPException(  # noqa: B904
            status_code=http_status,
            detail={"error": {"code": code, "message": message, "request_id": request_id}},
        )
    except Exception:
        raise HTTPException(  # noqa: B904
            status_code=500,
            detail={
                "error": {
                    "code": "BRAND_ANALYSIS_FAILED",
                    "message": "analysis failed",
                    "request_id": request_id,
                }
            },
        )


@router.get("", response_model=dict)
def get_brand(
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _perm: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    profile = load_brand_profile(tenant_id)
    if not profile:
        raise HTTPException(  # noqa: B904
            status_code=404,
            detail={
                "error": {
                    "code": "BRAND_NOT_FOUND",
                    "message": "no brand profile",
                    "request_id": request.headers.get("x-request-id", ""),
                }
            },
        )
    return profile


@router.get("/status", response_model=dict)
def get_brand_status(
    tenant_id: str = Depends(resolve_tenant_id),
    _perm: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    profile = load_brand_profile(tenant_id)
    return {"has_brand": profile is not None, "brand": profile}


@router.post("/manual", response_model=dict)
def post_manual_brand(
    body: ManualBrandRequest,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _admin: CurrentUser = Depends(require_permission("settings.update")),  # noqa: B008
):
    import re

    HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

    def _norm(v: str | None, required: bool = False) -> str | None:
        if v is None or str(v).strip() == "":
            # No primary entered: the default paper theme's heading colour (a stored
            # platform default), so the firm's paper matches a firm with no brand.
            return default_theme_colour("heading") if required else None
        s = str(v).strip()
        if not HEX_RE.match(s):
            raise HTTPException(  # noqa: B904
                status_code=400,
                detail={
                    "error": {
                        "code": "INVALID_COLOR",
                        "message": f"Invalid HEX color: {s}",
                        "request_id": request.headers.get("x-request-id", ""),
                    }
                },
            )
        return s.upper()

    primary = _norm(body.colors.get("primary"), required=True)
    secondary = _norm(body.colors.get("secondary"))
    accent = _norm(body.colors.get("accent"))

    colors = {"primary": primary, "secondary": secondary, "accent": accent}
    profile = manual_brand_profile(
        load_brand_profile(tenant_id),
        colors,
        body.website_url,
        body.name,
        default_typography=default_theme_fonts(),
    )
    saved = save_brand_profile(tenant_id, profile)
    return saved
