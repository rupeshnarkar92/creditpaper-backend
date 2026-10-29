from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import CurrentUser, require_permission
from creditpaper.onboarding.services.document_service import (
    MAX_DOCS,
    list_documents,
    store_and_process,
)
from creditpaper.onboarding.utils.store import (
    delete_paper as store_delete_paper,
)
from creditpaper.onboarding.utils.store import (
    load_documents,
    save_documents,
    validated_count,
)
from creditpaper.services.onboarding.pdf_section_extractor import extract_sections
from creditpaper.services.onboarding.pdf_style_extractor import extract_style

router = APIRouter(prefix="/api/onboarding/papers", tags=["onboarding"])

log = logging.getLogger(__name__)


def _parse_json_field(value, *, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return default


def _load_doc(tenant_id: str, document_id: str) -> dict:
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail="not found") from e  # noqa: B904
    for row in load_documents(tenant_id):
        if row.get("id") == str(doc_uuid):
            return row
    raise HTTPException(status_code=404, detail="not found")  # noqa: B904


def _read_payload(row: dict) -> dict:
    return {
        "sections": _parse_json_field(row.get("extracted_sections"), default=[]),
        "extracted_style": _parse_json_field(row.get("extracted_style"), default={}),
    }


def _public_document(row: dict) -> dict:
    """Never disclose host filesystem locations through an API response."""
    return {key: value for key, value in row.items() if key != "storage_path"}


@router.post("", response_model=dict)
async def upload_paper(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008
    tenant_id: str = Depends(resolve_tenant_id),
    _admin: CurrentUser = Depends(require_permission("settings.update")),  # noqa: B008
):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    try:
        content = await file.read()
        filename = file.filename or "upload.pdf"
        entry = await asyncio.to_thread(store_and_process, tenant_id, filename, content, file.content_type)
        count = await asyncio.to_thread(validated_count, tenant_id)
        return {
            "document": _public_document(entry),
            "validated_count": count,
            "required": 1,
            "max_documents": MAX_DOCS,
            "can_continue": count >= 1,
        }
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
        status = mapping.get(code, 400)
        raise HTTPException(  # noqa: B904
            status_code=status,
            detail={"error": {"code": code, "message": code, "request_id": request_id}},
        )
    except Exception:
        log.exception("upload_paper: store_and_process failed")
        raise HTTPException(  # noqa: B904
            status_code=500,
            detail={
                "error": {
                    "code": "DOCUMENT_PROCESSING_FAILED",
                    "message": "processing failed",
                    "request_id": request_id,
                }
            },
        )


@router.get("", response_model=dict)
def get_papers(
    tenant_id: str = Depends(resolve_tenant_id),
    _perm: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    docs = list_documents(tenant_id)
    count = sum(1 for d in docs if d.get("status") == "validated" and d.get("approval_detected"))
    return {
        "documents": [_public_document(doc) for doc in docs],
        "validated_count": count,
        "required": 1,
        "max_documents": MAX_DOCS,
        "can_continue": count >= 1,
    }


@router.delete("/{document_id}", response_model=dict)
def delete_paper(
    document_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
    _admin: CurrentUser = Depends(require_permission("settings.update")),  # noqa: B008
):
    # Atomic DELETE — avoids load/filter/save re-insert locks that caused pending.
    try:
        uuid.UUID(document_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail="not found") from e  # noqa: B904
    deleted = store_delete_paper(tenant_id, document_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="not found")  # noqa: B904
    # Papers changed → learned voice is invalid for this tenant.
    from creditpaper.onboarding.utils.store import delete_voice_profile

    delete_voice_profile(tenant_id)
    # Clear persisted voice generation state so stale "generating" never survives a delete.
    from creditpaper.db import tenant_cursor

    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT public.clear_voice_generation_state(%s)", (tenant_id,))
    return {"ok": True, "validated_count": validated_count(tenant_id)}


@router.get("/{document_id}/sections", response_model=dict)
def get_paper_sections(
    document_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
    _perm: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    return _read_payload(_load_doc(tenant_id, document_id))


@router.post("/{document_id}/re-extract", response_model=dict)
def re_extract_paper(
    document_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
    _admin: CurrentUser = Depends(require_permission("settings.update")),  # noqa: B008
):
    row = _load_doc(tenant_id, document_id)
    storage_path = row.get("storage_path")
    if not storage_path or not Path(storage_path).is_file():
        raise HTTPException(status_code=404, detail="stored file missing")  # noqa: B904
    try:
        from creditpaper.security.document_storage import read_document

        content = read_document(Path(storage_path), tenant_id=tenant_id, record_id=str(row["id"]))
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=404, detail="stored file unreadable") from e  # noqa: B904

    sections: list[dict] = []
    try:
        sections = extract_sections(content) or []
    except Exception:
        log.exception("re_extract: section extractor failed")
        sections = []

    style: dict = {"font_heading": None, "font_body": None, "primary_color": None}
    try:
        style = extract_style(content) or style
    except Exception:
        log.exception("re_extract: style extractor failed")

    row["extracted_sections"] = sections
    row["extracted_style"] = style
    docs = load_documents(tenant_id)
    docs = [row if d.get("id") == row.get("id") else d for d in docs]
    save_documents(tenant_id, docs)
    return _read_payload(row)
