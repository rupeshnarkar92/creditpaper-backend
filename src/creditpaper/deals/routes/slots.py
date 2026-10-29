"""Slot board routes (contract 2026-09-15 §A). Mounted under `/api/deals` through the
workspace router, so `api/main.py` gains no new engine-facing import.

  GET  /api/deals/{deal_id}/slots
  POST /api/deals/{deal_id}/documents/propose
  POST /api/deals/{deal_id}/slots/{slot_id}/confirm
  POST /api/deals/{deal_id}/slots/{slot_id}/change      {document_id}
  POST /api/deals/{deal_id}/slots/{slot_id}/assign      {document_id}
  POST /api/deals/{deal_id}/slots/{slot_id}/unassign    {document_id}
  POST /api/deals/{deal_id}/slots/{slot_id}/retry
  POST /api/deals/{deal_id}/documents/{document_id}/withdraw

The confirm-command route returns a small durable acknowledgment; legacy mutations
return the whole board. Every route resolves the tenant and scopes every
read to it; another tenant's deal, slot or document answers 404.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.errors import SerializationFailure
from pydantic import BaseModel, Field

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import require_permission
from creditpaper.auth.models.user import User
from creditpaper.db import tenant_uuid
from creditpaper.deals.services import slot_board

router = APIRouter(tags=["document-slots"])
logger = logging.getLogger("uvicorn.error")
_writer = Depends(require_permission("papers.update"))


class DocumentChoice(BaseModel):
    document_id: str = Field(..., min_length=1, max_length=64)


def _err(request: Request, code: str, message: str, status: int, field: str | None = None) -> HTTPException:
    body: dict[str, object] = {
        "code": code,
        "message": message,
        "request_id": request.headers.get("x-request-id", ""),
    }
    if field:
        body["field"] = field
    return HTTPException(status_code=status, detail={"error": body})


def _tenant(tenant_id: str) -> str:
    return str(tenant_uuid(tenant_id))


def _call(request: Request, deal_id: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except slot_board.DealNotFound:
        raise _err(request, "DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None
    except slot_board.DealTypeRequired:
        raise _err(
            request, "DEAL_TYPE_REQUIRED", slot_board.DealTypeRequired.message, 409, "deal_type"
        ) from None
    except slot_board.SlotNotFound:
        raise _err(request, "SLOT_NOT_FOUND", "slot not found", 404, "slot_id") from None
    except slot_board.DocumentNotFound:
        raise _err(request, "DOCUMENT_NOT_FOUND", "document not found", 404, "document_id") from None
    except slot_board.SlotConflict as exc:
        raise _err(request, exc.code, exc.message, 409) from None
    except SerializationFailure:
        raise _err(
            request,
            "SLOT_PROPOSAL_CHANGED",
            "This proposal changed. Refresh the documents before confirming it.",
            409,
        ) from None


def _schedule(tenant_id: str, deal_id: str, jobs: list) -> None:
    """Confirmed slots extract through the durable processing queue (leases, retries, dedupe)."""
    for job in jobs:
        slot_board.enqueue_slot_extraction(tenant_id, deal_id, job)


@router.get("/{deal_id}/slots")
def get_slots(deal_id: str, request: Request, tenant_id: str = Depends(resolve_tenant_id)):
    tid = _tenant(tenant_id)
    started = time.perf_counter()
    result = _call(request, deal_id, lambda: slot_board.get_board(tid, deal_id))
    logger.info("slots_get_board_ms=%.2f", (time.perf_counter() - started) * 1000)
    return result


@router.post("/{deal_id}/documents/propose")
def post_propose(
    deal_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    tid = _tenant(tenant_id)
    return _call(request, deal_id, lambda: slot_board.propose(tid, deal_id, str(user.id)))


@router.post("/{deal_id}/slots/{slot_id}/confirm-command")
def post_confirm_command(
    deal_id: str,
    slot_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    return _call(
        request,
        deal_id,
        lambda: slot_board.confirm_command(_tenant(tenant_id), deal_id, slot_id, str(user.id)),
    )


@router.post("/{deal_id}/slots/{slot_id}/confirm")
def post_confirm(
    deal_id: str,
    slot_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    tid = _tenant(tenant_id)
    board, jobs = _call(request, deal_id, lambda: slot_board.confirm(tid, deal_id, slot_id, str(user.id)))
    queued_at = time.perf_counter()
    _schedule(tid, deal_id, jobs)
    logger.info("slot_confirm_queue_ms=%.2f jobs=%d", (time.perf_counter() - queued_at) * 1000, len(jobs))
    return board


@router.post("/{deal_id}/slots/{slot_id}/change")
def post_change(
    deal_id: str,
    slot_id: str,
    body: DocumentChoice,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    tid = _tenant(tenant_id)
    board, jobs = _call(
        request,
        deal_id,
        lambda: slot_board.change(tid, deal_id, slot_id, body.document_id, str(user.id)),
    )
    _schedule(tid, deal_id, jobs)
    return board


@router.post("/{deal_id}/slots/{slot_id}/assign")
def post_assign(
    deal_id: str,
    slot_id: str,
    body: DocumentChoice,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    tid = _tenant(tenant_id)
    board, jobs = _call(
        request,
        deal_id,
        lambda: slot_board.assign(tid, deal_id, slot_id, body.document_id, str(user.id)),
    )
    _schedule(tid, deal_id, jobs)
    return board


@router.post("/{deal_id}/slots/{slot_id}/unassign")
def post_unassign(
    deal_id: str,
    slot_id: str,
    body: DocumentChoice,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    tid = _tenant(tenant_id)
    return _call(
        request,
        deal_id,
        lambda: slot_board.unassign(tid, deal_id, slot_id, body.document_id, str(user.id)),
    )


@router.post("/{deal_id}/slots/{slot_id}/retry")
def post_retry(
    deal_id: str,
    slot_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    tid = _tenant(tenant_id)
    board, jobs = _call(request, deal_id, lambda: slot_board.retry(tid, deal_id, slot_id, str(user.id)))
    _schedule(tid, deal_id, jobs)
    return board


@router.post("/{deal_id}/documents/{document_id}/withdraw")
def post_withdraw(
    deal_id: str,
    document_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
):
    tid = _tenant(tenant_id)
    return _call(request, deal_id, lambda: slot_board.withdraw(tid, deal_id, document_id, str(user.id)))
