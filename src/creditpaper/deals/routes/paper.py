"""Template choice, paper theme and logo routes (spec 2026-09-15 §2, §4).

  GET  /api/deals/template-options?deal_type=bridge
  GET  /api/deals/template-options/{template_id}/outline
  PUT  /api/deals/{deal_id}/template
  GET  /api/deals/{deal_id}/paper/theme
  GET  /api/deals/{deal_id}/paper/logo
  GET  /api/paper/theme          (deal-free: onboarding and Library previews)
  GET  /api/paper/logo           (deal-free)

Every route resolves the tenant through `resolve_tenant_id` and scopes every read to
it; a deal or template from another tenant is indistinguishable from one that does
not exist. `template_options_router` must be mounted before the deals CRUD router,
whose `GET /api/deals/{deal_id}` would otherwise capture `template-options`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import require_permission
from creditpaper.auth.models.user import User
from creditpaper.deals.services import deal_templates
from creditpaper.deals.store import deal_template_store
from creditpaper.paper import logo_fetch, theme

log = logging.getLogger(__name__)

template_options_router = APIRouter(prefix="/api/deals", tags=["deal-templates"])
deal_paper_router = APIRouter(prefix="/api/deals", tags=["deal-templates"])
paper_router = APIRouter(prefix="/api/paper", tags=["paper-theme"])
_writer = Depends(require_permission("papers.update"))

_DEAL_TYPE_PATTERN = r"^(construction|bridge|residual_stock)$"


class SectionEditRequest(BaseModel):
    editor_value: list[dict[str, Any]] = Field(min_length=1, max_length=20000)
    expected_revision: int = Field(ge=0)


@deal_paper_router.put("/{deal_id}/paper/sections/{key}")
def put_paper_section(
    deal_id: str,
    key: str,
    body: SectionEditRequest,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    from creditpaper.deals.services.section_edits import EditError, save_section

    try:
        return save_section(tenant_id, deal_id, key, str(user.id), body.editor_value, body.expected_revision)
    except EditError as exc:
        raise _err(request, exc.code, str(exc), exc.status) from None
    except ValueError as exc:
        raise _err(request, "INVALID_EDITOR_DOCUMENT", str(exc), 422) from None


def _err(request: Request, code: str, message: str, status: int, field: str | None = None) -> HTTPException:
    body: dict[str, object] = {
        "code": code,
        "message": message,
        "request_id": request.headers.get("x-request-id", ""),
    }
    if field:
        body["field"] = field
    return HTTPException(status_code=status, detail={"error": body})


def _deal_not_found(request: Request, deal_id: str) -> HTTPException:
    return _err(request, "DEAL_NOT_FOUND", f"deal {deal_id} not found", 404)


def template_unavailable(request: Request) -> HTTPException:
    return _err(
        request,
        "TEMPLATE_UNAVAILABLE",
        deal_templates.TEMPLATE_UNAVAILABLE_MESSAGE,
        422,
        field="template_id",
    )


# --- template options -----------------------------------------------------------


@template_options_router.get("/template-options")
def get_template_options(
    deal_type: str | None = Query(default=None, pattern=_DEAL_TYPE_PATTERN),
    tenant_id: str = Depends(resolve_tenant_id),
):
    return deal_templates.list_template_options(tenant_id, deal_type)


@template_options_router.get("/template-options/{template_id}/outline")
def get_template_outline(
    template_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
):
    try:
        return deal_templates.get_outline(tenant_id, template_id)
    except deal_templates.TemplateUnavailable:
        raise template_unavailable(request) from None


class ChangeTemplateRequest(BaseModel):
    template_id: str = Field(..., min_length=1, max_length=64)


@deal_paper_router.put("/{deal_id}/template")
def put_deal_template(
    deal_id: str,
    body: ChangeTemplateRequest,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    try:
        return deal_templates.change_template(tenant_id, deal_id, body.template_id, str(user.id))
    except deal_templates.DealNotFound:
        raise _deal_not_found(request, deal_id) from None
    except deal_templates.TemplateUnavailable:
        raise template_unavailable(request) from None
    except deal_templates.TemplateLocked:
        raise _err(
            request,
            "TEMPLATE_LOCKED",
            "This paper has been generated, so its template can no longer be changed.",
            409,
        ) from None


# --- theme and logo --------------------------------------------------------------


def _require_deal(request: Request, tenant_id: str, deal_id: str) -> None:
    if deal_template_store.get_deal(tenant_id, deal_id) is None:
        raise _deal_not_found(request, deal_id)


def deal_logo_path(deal_id: str) -> str:
    return f"/api/deals/{deal_id}/paper/logo"


TENANT_LOGO_PATH = "/api/paper/logo"


@deal_paper_router.get("/{deal_id}/paper/theme")
def get_deal_paper_theme(deal_id: str, request: Request, tenant_id: str = Depends(resolve_tenant_id)):
    _require_deal(request, tenant_id, deal_id)
    return theme.get_theme(tenant_id, logo_endpoint=deal_logo_path(deal_id))


@paper_router.get("/theme")
def get_paper_theme(tenant_id: str = Depends(resolve_tenant_id)):
    return theme.get_theme(tenant_id, logo_endpoint=TENANT_LOGO_PATH)


def _logo_response(request: Request, tenant_id: str) -> Response:
    brand = theme.load_brand(tenant_id)
    url = theme.brand_logo_url(brand)
    logo = logo_fetch.fetch_logo(url) if url else None
    if logo is None:
        raise _err(request, "LOGO_NOT_FOUND", "no logo available", 404)
    headers = {
        "Cache-Control": "private, max-age=3600",
        "ETag": logo.etag,
        "X-Content-Type-Options": "nosniff",
        # An SVG opened directly must not run script or load anything.
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    }
    if request.headers.get("if-none-match") == logo.etag:
        return Response(status_code=304, headers=headers)
    return Response(content=logo.content, media_type=logo.content_type, headers=headers)


@deal_paper_router.get("/{deal_id}/paper/logo")
def get_deal_paper_logo(deal_id: str, request: Request, tenant_id: str = Depends(resolve_tenant_id)):
    _require_deal(request, tenant_id, deal_id)
    return _logo_response(request, tenant_id)


@paper_router.get("/logo")
def get_paper_logo(request: Request, tenant_id: str = Depends(resolve_tenant_id)):
    return _logo_response(request, tenant_id)
