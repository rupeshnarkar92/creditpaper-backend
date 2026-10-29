"""Edit history and discussion routes for the current paper workspace."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import WRITER
from creditpaper.auth.models.user import User
from creditpaper.db import tenant_cursor, tenant_uuid
from creditpaper.deals.services import paper_workflow

router = APIRouter(prefix="/api/deals", tags=["paper-workflow"])
_writer = Depends(WRITER)


class RestoreRequest(BaseModel):
    expected_revision: int = Field(ge=0)


class CommentRequest(BaseModel):
    body: Annotated[str, Field(min_length=1, max_length=10000)]
    thread_id: UUID | None = None

    @field_validator("body")
    @classmethod
    def body_must_not_be_blank(cls, body: str) -> str:
        body = body.strip()
        if not body:
            raise ValueError("Comment body cannot be blank")
        return body


def _error(request: Request, code: str, message: str, status: int) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={
            "error": {
                "code": code,
                "message": message,
                "request_id": request.headers.get("x-request-id", ""),
            }
        },
    )


@router.get("/{deal_id}/paper/sections/{section_key}/history")
def get_edit_history(
    deal_id: str,
    section_key: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _user: User = _writer,
):
    tid = str(tenant_uuid(tenant_id))
    try:
        section = paper_workflow.engine_section_number(tid, deal_id, section_key)
    except ValueError:
        raise _error(request, "PAPER_SECTION_NOT_FOUND", "Paper section not found.", 404) from None
    return {"history": paper_workflow.list_edit_history(tid, deal_id, section)}


@router.post("/{deal_id}/paper/sections/{section_key}/history/{history_id}/restore")
def restore_edit(
    deal_id: str,
    section_key: str,
    history_id: str,
    body: RestoreRequest,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    tid = str(tenant_uuid(tenant_id))
    try:
        section = paper_workflow.engine_section_number(tid, deal_id, section_key)
    except ValueError:
        raise _error(request, "PAPER_SECTION_NOT_FOUND", "Paper section not found.", 404) from None
    revision = paper_workflow.restore_edit(
        tid, deal_id, section, history_id, body.expected_revision, str(user.id)
    )
    if revision is None:
        raise _error(
            request,
            "PAPER_EDIT_REVISION_MISMATCH",
            "The section changed or that history entry is unavailable.",
            409,
        )
    return {"section": section, "edit_revision": revision}


@router.get("/{deal_id}/paper/comments")
def get_comments(
    deal_id: str,
    include_resolved: bool = True,
    tenant_id: str = Depends(resolve_tenant_id),
    _user: User = _writer,
):
    threads = paper_workflow.list_comment_threads(
        str(tenant_uuid(tenant_id)), deal_id, include_resolved=include_resolved
    )
    return {"threads": threads}


@router.post("/{deal_id}/paper/sections/{section_key}/comments")
def post_comment(
    deal_id: str,
    section_key: str,
    body: CommentRequest,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    thread_id = paper_workflow.add_comment(
        str(tenant_uuid(tenant_id)), deal_id, section_key, str(user.id), body.body, body.thread_id
    )
    if thread_id is None:
        raise _error(
            request,
            "PAPER_SECTION_NOT_FOUND",
            "The paper section or comment thread was not found.",
            404,
        )
    return {"thread_id": thread_id}


class GuardrailSignoffRequest(BaseModel):
    section: int = Field(ge=1, le=9)
    rule_id: UUID
    result: str = Field(pattern="^(flagged|error)$")
    reason: Annotated[str, Field(min_length=1, max_length=2000)]

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, reason: str) -> str:
        reason = reason.strip()
        if not reason:
            raise ValueError("Sign-off reason cannot be blank")
        return reason


@router.post("/{deal_id}/paper/guardrails/signoffs")
def sign_off_guardrail(
    deal_id: str,
    body: GuardrailSignoffRequest,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT public.record_guardrail_signoff(%s, %s, %s, %s, %s, %s, %s)",
            (tid, deal_id, body.section, body.rule_id, body.result, user.id, body.reason),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        raise _error(
            request,
            "GUARDRAIL_SIGNOFF_UNAVAILABLE",
            "This blocking result is not available for sign-off.",
            409,
        )
    return {"section": body.section, "rule_id": body.rule_id, "result": body.result, "signed_off": True}


@router.post("/{deal_id}/paper/comments/{thread_id}/resolve")
def resolve_comment(
    deal_id: str,
    thread_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    if not paper_workflow.resolve_comment(str(tenant_uuid(tenant_id)), deal_id, thread_id, str(user.id)):
        raise _error(request, "PAPER_COMMENT_NOT_FOUND", "The open comment thread was not found.", 404)
    return {"thread_id": thread_id, "resolved": True}
