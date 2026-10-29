"""Templates API routes — 11 endpoints under /api/templates.

All routes resolve the tenant via the X-Tenant-ID header (or auth token)
via the same `resolve_tenant_id` dependency used elsewhere.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import CurrentUser, require_permission
from creditpaper.templates.schemas import (
    SaveSectionsRequest,
    TemplateCreate,
    TemplateUpdate,
)
from creditpaper.templates.services import template_service

router = APIRouter(prefix="/templates", tags=["templates"])
_writer_create = Depends(require_permission("templates.create"))
_writer_update = Depends(require_permission("templates.update"))
_writer_delete = Depends(require_permission("templates.delete"))
_reader = Depends(require_permission("templates.read"))
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or str(uuid.uuid4())


def _err(code: str, message: str, request_id: str, http_status: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http_status,
        detail={"error": {"code": code, "message": message, "request_id": request_id}},
    )


# ---------------------------------------------------------------------------
# GET /api/templates/paper-types  (must be defined BEFORE /{template_id})
# ---------------------------------------------------------------------------


@router.get("/paper-types", response_model=dict)
def get_paper_types(
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: CurrentUser = _reader,
):
    rid = _request_id(request)
    try:
        return {"types": template_service.list_paper_types()}
    except Exception:  # noqa: BLE001
        log.exception("list_paper_types failed")
        raise _err("PAPER_TYPES_LIST_FAILED", "failed to list paper types", rid, 500) from None


# ---------------------------------------------------------------------------
# GET /api/templates
# ---------------------------------------------------------------------------


@router.get("", response_model=dict)
def get_templates(
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: CurrentUser = _reader,
):
    rid = _request_id(request)
    try:
        return template_service.list_templates(tenant_id)
    except Exception:  # noqa: BLE001
        log.exception("list_templates failed")
        raise _err("TEMPLATES_LIST_FAILED", "failed to list templates", rid, 500) from None


# ---------------------------------------------------------------------------
# POST /api/templates
# ---------------------------------------------------------------------------


@router.post("", response_model=dict)
def post_template(
    body: TemplateCreate,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _writer_user: CurrentUser = _writer_create,
):
    rid = _request_id(request)
    try:
        return template_service.create_template(
            tenant_id,
            name=body.name,
            description=body.description,
            paper_types=body.paper_types,
        )
    except ValueError as e:
        if str(e) == "DUPLICATE_PAPER_TYPE_TEMPLATE":
            raise _err(
                "DUPLICATE_PAPER_TYPE",
                "Template already created for this paper type",
                rid,
                409,
            ) from e
        raise _err("INVALID_TEMPLATE", str(e), rid, 400) from e
    except Exception:  # noqa: BLE001
        log.exception("create_template failed")
        raise _err("TEMPLATE_CREATE_FAILED", "create failed", rid, 500) from None


# ---------------------------------------------------------------------------
# GET /api/templates/{template_id}
# ---------------------------------------------------------------------------


@router.get("/{template_id}", response_model=dict)
def get_template(
    template_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: CurrentUser = _reader,
):
    rid = _request_id(request)
    t = template_service.get_template(tenant_id, template_id)
    if not t:
        raise _err("TEMPLATE_NOT_FOUND", "template not found", rid, 404)
    return t


# ---------------------------------------------------------------------------
# PATCH /api/templates/{template_id}
# ---------------------------------------------------------------------------


@router.patch("/{template_id}", response_model=dict)
def patch_template(
    template_id: str,
    body: TemplateUpdate,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _writer_user: CurrentUser = _writer_update,
):
    rid = _request_id(request)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    try:
        updated = template_service.update_template(tenant_id, template_id, **fields)
    except ValueError as e:
        if str(e) == "DUPLICATE_PAPER_TYPE_TEMPLATE":
            raise _err(
                "DUPLICATE_PAPER_TYPE",
                "Template already created for this paper type",
                rid,
                409,
            ) from e
        if str(e) == "TEMPLATE_NOT_EDITABLE":
            raise _err(
                "TEMPLATE_NOT_EDITABLE", "Default and predefined templates cannot be edited", rid, 403
            ) from e
        raise _err("INVALID_TEMPLATE", str(e), rid, 400) from e
    except Exception:  # noqa: BLE001
        log.exception("update_template failed")
        raise _err("TEMPLATE_UPDATE_FAILED", "update failed", rid, 500) from None
    if not updated:
        raise _err("TEMPLATE_NOT_FOUND", "template not found", rid, 404)
    return updated


# ---------------------------------------------------------------------------
# DELETE /api/templates/{template_id}
# ---------------------------------------------------------------------------


@router.delete("/{template_id}", response_model=dict)
def delete_template(
    template_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _writer_user: CurrentUser = _writer_delete,
):
    rid = _request_id(request)
    try:
        ok = template_service.delete_template(tenant_id, template_id)
    except ValueError as e:
        raise _err("DEFAULT_TEMPLATE_NOT_DELETABLE", str(e), rid, 400) from e
    except Exception:  # noqa: BLE001
        log.exception("delete_template failed")
        raise _err("TEMPLATE_DELETE_FAILED", "delete failed", rid, 500) from None
    if not ok:
        raise _err("TEMPLATE_NOT_FOUND", "template not found", rid, 404)
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /api/templates/{template_id}/activate
# ---------------------------------------------------------------------------


@router.post("/{template_id}/activate", response_model=dict)
def post_activate(
    template_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _writer_user: CurrentUser = _writer_update,
):
    rid = _request_id(request)
    t = template_service.activate_template(tenant_id, template_id)
    if not t:
        raise _err("TEMPLATE_NOT_FOUND", "template not found", rid, 404)
    return {"ok": True, "template": t}


# ---------------------------------------------------------------------------
# GET /api/templates/{template_id}/papers
# ---------------------------------------------------------------------------


@router.get("/{template_id}/papers", response_model=dict)
def get_papers(
    template_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: CurrentUser = _reader,
):
    rid = _request_id(request)
    try:
        return {"documents": template_service.list_papers(tenant_id, template_id)}
    except Exception:  # noqa: BLE001
        log.exception("list_papers failed")
        raise _err("PAPERS_LIST_FAILED", "failed to list papers", rid, 500) from None


@router.get("/{template_id}/papers/sections", response_model=dict)
def get_papers_sections(
    template_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: CurrentUser = _reader,
):
    rid = _request_id(request)
    try:
        return {"sections": template_service.get_papers_sections(tenant_id, template_id)}
    except Exception:  # noqa: BLE001
        log.exception("papers sections failed")
        raise _err("PAPERS_SECTIONS_FAILED", "failed to load sections", rid, 500) from None


# ---------------------------------------------------------------------------
# POST /api/templates/{template_id}/papers  (file upload)
# ---------------------------------------------------------------------------


@router.post("/{template_id}/papers", response_model=dict)
async def post_paper(
    template_id: str,
    request: Request,
    file: UploadFile = File(...),  # noqa: B008
    tenant_id: str = Depends(resolve_tenant_id),
    _writer_user: CurrentUser = _writer_update,
):
    rid = _request_id(request)
    try:
        content = await file.read()
        if not content:
            raise _err("FILE_EMPTY", "uploaded file is empty", rid, 400)
        entry = await asyncio.to_thread(
            template_service.upload_paper,
            tenant_id,
            template_id,
            filename=file.filename or "upload.pdf",
            content=content,
            content_type=file.content_type,
        )
        return {"document": entry, "can_continue": True}
    except ValueError as e:
        code = str(e)
        mapping = {
            "FILE_TYPE_NOT_SUPPORTED": 400,
            "FILE_TOO_LARGE": 413,
            "FILE_CORRUPTED": 400,
            "NOT_A_CREDIT_PAPER": 422,
            "APPROVAL_NOT_DETECTED": 422,
            "MAX_DOCUMENTS_REACHED": 422,
        }
        raise _err(code, code, rid, mapping.get(code, 400)) from e
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        log.exception("upload_paper failed")
        raise _err("PAPER_UPLOAD_FAILED", "upload failed", rid, 500) from None


# ---------------------------------------------------------------------------
# DELETE /api/templates/{template_id}/papers/{paper_id}
# ---------------------------------------------------------------------------


@router.delete("/{template_id}/papers/{paper_id}", response_model=dict)
def delete_paper(
    template_id: str,
    paper_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _writer_user: CurrentUser = _writer_update,
):
    rid = _request_id(request)
    try:
        ok = template_service.delete_paper(tenant_id, template_id, paper_id)
    except Exception:  # noqa: BLE001
        log.exception("delete_paper failed")
        raise _err("PAPER_DELETE_FAILED", "delete failed", rid, 500) from None
    if not ok:
        raise _err("PAPER_NOT_FOUND", "paper not found", rid, 404)
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /api/templates/{template_id}/sections
# ---------------------------------------------------------------------------


@router.post("/{template_id}/sections", response_model=dict)
def post_sections(
    template_id: str,
    body: SaveSectionsRequest,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _writer_user: CurrentUser = _writer_update,
):
    rid = _request_id(request)
    sections = [s.model_dump() for s in body.sections]
    try:
        updated = template_service.save_sections(tenant_id, template_id, sections)
    except ValueError as e:
        if str(e) == "TEMPLATE_NOT_EDITABLE":
            raise _err(
                "TEMPLATE_NOT_EDITABLE", "Default and predefined templates cannot be edited", rid, 403
            ) from e
        raise _err("INVALID_TEMPLATE", str(e), rid, 400) from e
    except Exception:  # noqa: BLE001
        log.exception("save_sections failed")
        raise _err("SECTIONS_SAVE_FAILED", "save failed", rid, 500) from None
    if not updated:
        raise _err("TEMPLATE_NOT_FOUND", "template not found", rid, 404)
    return {"ok": True, "count": len(sections), "template": updated}


# ---------------------------------------------------------------------------
# GET /api/templates/{template_id}/preview-doc
# ---------------------------------------------------------------------------


@router.get("/{template_id}/preview-doc")
def get_preview_doc(
    template_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: CurrentUser = _reader,
):
    rid = _request_id(request)
    body = template_service.render_preview_doc(tenant_id, template_id)
    if not body:
        raise _err("PREVIEW_RENDER_FAILED", "failed to render preview", rid, 500)
    headers = {"Content-Disposition": f'attachment; filename="template_{template_id}_preview.docx"'}
    return StreamingResponse(
        iter([body]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers,
    )
