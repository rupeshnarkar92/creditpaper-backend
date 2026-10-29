"""HTTP routes for the Deal Workspace slice 1.

Mounted under `/api/deals` in `api/main.py`. Thin wrappers over
`services.workspace`; the service is the source of truth for state
transitions, audit, and document version chain.

All errors return the project's standard `error_code` envelope
(`{error: {code, message, request_id}}`). New codes are prefixed
`DEAL_` and listed in `docs/handbook/DEAL_WORKSPACE.md`.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import APPROVER_ROLES, require_any_permission, require_permission
from creditpaper.auth.models.user import User
from creditpaper.db import tenant_uuid
from creditpaper.deals.services.ingestion_gate import require_ready
from creditpaper.deals.services.workspace import (
    DealDocumentDuplicate,
    DealDocumentEmpty,
    DealDocumentTooLarge,
    DealDocumentTypeUnsupported,
    DealInvalidTransition,
    DealNotFound,
    DealRevisionMismatch,
    get_workspace_payload,
    stream_version,
)
from creditpaper.deals.services.workspace import (
    transition as transition_service,
)
from creditpaper.deals.services.workspace import (
    upload_document as upload_document_service,
)
from creditpaper.deals.store.workspace_store import (
    extraction_status_snapshot,
    latest_document_version_id,
    list_audit,
    list_transitions,
    version_document_id,
    workspace_exists,
)
from creditpaper.obs.timing import timed

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/deals", tags=["deals-workspace"])
# Gates name permissions, never roles: a grant change is a seed edit (2026-09-25).
_workspace_writer = Depends(require_permission("papers.update"))
# Review decisions and stage moves are the reviewer's as well as the drafter's.
_workspace_reviewer = Depends(require_any_permission("papers.update", "papers.approve"))


class UploadBatchRequest(BaseModel):
    id: str
    total: int = Field(ge=1, le=1000)
    failed: int = Field(default=0, ge=0, le=1000)
    sealed: bool = False


@router.get("/{deal_id}/ingestion-status")
def ingestion_status(deal_id: str, tenant_id: str = Depends(resolve_tenant_id)):
    from creditpaper.deals.services.ingestion_gate import status

    return status(tenant_id, deal_id)


@router.post("/{deal_id}/upload-batches")
def update_upload_batch(
    deal_id: str,
    body: UploadBatchRequest,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _workspace_writer,
):
    from uuid import UUID

    from creditpaper.db import tenant_cursor

    try:
        batch_id = UUID(body.id)
    except ValueError:
        raise HTTPException(422, "Invalid batch ID") from None
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.deal_upload_batch_write(%s::uuid,%s,%s,%s,%s,%s)",
            (tenant_id, deal_id, batch_id, body.total, body.failed, body.sealed),
        )
    return {"id": str(batch_id)}


@router.post("/{deal_id}/ingestion-acknowledge")
def acknowledge_ingestion(
    deal_id: str, tenant_id: str = Depends(resolve_tenant_id), user: User = _workspace_writer
):
    from psycopg.errors import RaiseException

    from creditpaper.db import tenant_cursor

    try:
        with tenant_cursor(tenant_id) as cur:
            cur.execute("SELECT public.deal_ingestion_acknowledge(%s::uuid,%s)", (tenant_id, deal_id))
            from creditpaper.deals.services.activity import record_deal_event

            record_deal_event(
                cur,
                tenant_id=tenant_id,
                deal_id=deal_id,
                actor_id=str(user.id),
                action="document.failures_acknowledged",
                metadata={"explicit": True},
            )
    except RaiseException:
        raise HTTPException(
            409, "Upload still active; wait before acknowledging interrupted uploads."
        ) from None
    return {"acknowledged": True}


@router.post("/{deal_id}/upload-batches/{batch_id}/archive", status_code=202)
async def upload_archive(
    deal_id: str,
    batch_id: str,
    file: UploadFile = File(...),  # noqa: B008
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _workspace_writer,
):
    import tempfile
    import zipfile
    from pathlib import Path
    from uuid import UUID

    from psycopg.errors import RaiseException
    from starlette.concurrency import run_in_threadpool

    from creditpaper.deals.services.archive_intake import MAX_ARCHIVE_BYTES, accept_path
    from creditpaper.ledger.jobs import QueueOverloaded

    try:
        batch_id = str(UUID(batch_id))
        size = 0
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
                temporary = Path(handle.name)
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise ValueError("ZIP exceeds 256 MB; split the folder into smaller batches")
                    handle.write(chunk)
            job_id = await run_in_threadpool(
                accept_path,
                str(tenant_uuid(tenant_id)),
                deal_id,
                batch_id,
                str(user.id),
                temporary,
            )
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except (ValueError, zipfile.BadZipFile) as exc:
        raise HTTPException(422, detail={"error": {"code": "INVALID_ARCHIVE", "message": str(exc)}}) from None
    except RaiseException:
        raise HTTPException(409, "Batch unavailable, already accepted, or generation active") from None
    except QueueOverloaded as exc:
        raise HTTPException(
            429,
            detail={"error": {"code": "PROCESSING_QUEUE_FULL", "message": str(exc)}},
            headers={"Retry-After": "15"},
        ) from None
    return {"batch_id": batch_id, "job_id": job_id, "status": "queued"}


@router.post("/{deal_id}/upload-batches/{batch_id}/retry", status_code=202)
def retry_archive(
    deal_id: str, batch_id: str, tenant_id: str = Depends(resolve_tenant_id), user: User = _workspace_writer
):
    from uuid import UUID

    from psycopg.errors import RaiseException

    from creditpaper.db import tenant_cursor
    from creditpaper.ledger import jobs

    try:
        batch_id = str(UUID(batch_id))
        with tenant_cursor(tenant_id) as cur:
            cur.execute(
                "SELECT public.retry_document_archive(%s::uuid,%s,%s::uuid,%s::uuid)",
                (tenant_id, deal_id, batch_id, str(user.id)),
            )
            job_id = str(cur.fetchone()[0])
    except ValueError:
        raise HTTPException(422, "Invalid batch ID") from None
    except RaiseException:
        raise HTTPException(409, "Batch is unavailable or processing is active") from None
    jobs.dispatch(tenant_id, job_id)
    return {"job_id": job_id}


def _approver(user: User) -> bool:
    """Whether this person may move a paper to APPROVED or ISSUED.

    Reads the resolved grant, not the role: `papers.approve` is the whole test, so a
    firm that grants it outside the checker role is honoured without a code change.
    """
    return user.is_super_admin or "papers.approve" in user.permissions


# The states a reviewer decides. Every other stage move is the drafter's.
_DECISION_STATES = {"APPROVED", "ISSUED"}


def _may_move_to(user: User, to: str) -> bool:
    """Approval needs `papers.approve`; any other stage change needs `papers.update`."""
    if to in _DECISION_STATES:
        return _approver(user)
    return user.is_super_admin or "papers.update" in user.permissions


@timed("processing")
def _process_version_safe(version_id: str, deal_id: str = "", tenant_id: str = "") -> str:
    """Durably enqueue parsing before upload acknowledgement; extraction waits on a placement."""
    from creditpaper.ledger import jobs

    document_id = version_document_id(tenant_id, deal_id, version_id)
    if document_id is None:
        raise _http_error("DOCUMENT_NOT_FOUND", "Document not found", 404)
    job_id = jobs.enqueue(
        str(tenant_id),
        deal_id,
        "parse",
        version_id,
        {"version_id": version_id, "document_id": document_id},
    )
    jobs.dispatch(str(tenant_id), job_id)
    return job_id


class TransitionRequest(BaseModel):
    to: str = Field(..., min_length=1)
    reason: str | None = None
    expected_revision: int | None = None
    acknowledged_outstanding_item_ids: list[str] = Field(default_factory=list, max_length=200)
    acknowledged_contradiction_ids: list[str] = Field(default_factory=list, max_length=200)


def _can_approve(user: User) -> bool:
    """Checker authority comes from any held role, never from the primary one."""
    if getattr(user, "is_super_admin", False):
        return True
    roles = getattr(user, "roles", None)
    if isinstance(roles, str):
        roles = [roles]
    if not roles:
        # Legacy single-role users (`auth.models.user.User`) have no `roles`.
        roles = [user.role] if user.role else []
    return any(role in APPROVER_ROLES for role in roles)


def _http_error(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": {"code": code, "message": message}})


@router.get("/{deal_id}/approval-readiness")
def get_approval_readiness(
    deal_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _workspace_writer,
):
    from creditpaper.ledger.service import approval_readiness

    return approval_readiness(str(tenant_uuid(tenant_id)), deal_id)


@router.get("/{deal_id}/workspace")
def get_workspace(
    deal_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
):
    started = time.perf_counter()
    try:
        result = get_workspace_payload(tenant_id=tenant_uuid(tenant_id), deal_id=deal_id)
        log.info("workspace_api_ms=%.2f", (time.perf_counter() - started) * 1000)
        return result
    except DealNotFound:
        raise _http_error("DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None


@router.get("/{deal_id}/page")
def get_workspace_page(
    deal_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
):
    """Mega-endpoint: returns all workspace page data in one round-trip.

    Replaces 8 separate calls (workspace + slots + fact-sheet + activity +
    reviews + sections + theme + recommendation) with a single SP call.
    """
    from creditpaper.deals.services.workspace_page import build_workspace_page
    from creditpaper.deals.store.slot_store import call_workspace_page

    started = time.perf_counter()
    try:
        raw = call_workspace_page(tenant_id=tenant_uuid(tenant_id), deal_id=deal_id)
        loaded = time.perf_counter()
        payload = build_workspace_page(str(tenant_uuid(tenant_id)), deal_id, raw)
        log.info(
            "workspace_page_api db_and_pool_s=%.3f assemble_s=%.3f total_s=%.3f",
            loaded - started,
            time.perf_counter() - loaded,
            time.perf_counter() - started,
        )
        return payload
    except DealNotFound:
        raise _http_error("DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None
    except Exception:
        raise _http_error("DATABASE_UNAVAILABLE", "retry safely", 503) from None


@router.get("/{deal_id}/facts")
def get_facts(
    deal_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
):
    """Return today's time-scoped model projection for the Fact Sheet."""
    require_ready(tenant_id, deal_id, capability="facts")
    from creditpaper.ledger.service import load_model

    model = load_model(tenant_id, deal_id)
    facts = _flatten_facts(model.model_dump(mode="json")) if model else []
    return {"facts": facts, "total": len(facts)}


def _flatten_facts(model: dict) -> list[dict]:
    """Flatten the stored DealModel into a list of facts.

    Walks every block of the model (it used to read a hard-coded ten and dropped the
    rest). Field paths keep their historical dotted form for existing callers; the
    fact sheet (`GET /fact-sheet`) is the analyst-facing view.
    """
    from creditpaper.deals.services.fact_walk import walk_facts

    def _format_provenance(prov: dict | None) -> str:
        if not prov:
            return "—"
        doc_id = prov.get("document_id", "")
        page = prov.get("page")
        location = prov.get("location", "")
        if page:
            return f"{doc_id}, p.{page}"
        return doc_id or location or "—"

    if not isinstance(model, dict):
        return []
    return [
        {
            "field": path,
            "value": fact.get("value"),
            "status": fact.get("status", "missing"),
            "source": _format_provenance(fact.get("provenance")),
            "confidence": fact.get("confidence"),
            "document_id": (fact.get("provenance") or {}).get("document_id"),
            "provenance": fact.get("provenance"),
            "assertion_id": fact.get("assertion_id"),
            "valid_from": fact.get("valid_from"),
            "valid_to": fact.get("valid_to"),
        }
        for path, fact in walk_facts(model)
    ]


@router.post("/{deal_id}/transitions")
def post_transition(
    deal_id: str,
    body: TransitionRequest,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _workspace_reviewer,
):
    actor_id = str(user.id)
    # SEC-13: approval authority must not come from `user.role` — it is only the
    # alphabetically-first role and says nothing about the others, so reading it
    # refused `maker + tenant_admin` on a real approver. `_may_move_to` reads the
    # resolved grant (`papers.approve` / `papers.update`) instead, which already
    # covers every role the user holds.
    if not _may_move_to(user, body.to):
        if body.to in _DECISION_STATES:
            raise _http_error("APPROVAL_FORBIDDEN", "Approval permission required", 403)
        raise _http_error("TRANSITION_FORBIDDEN", "Stage changes need papers.update", 403)
    audit_extra = _approval_audit_metadata(tenant_id, deal_id) if body.to == "APPROVED" else None
    try:
        return transition_service(
            tenant_id=tenant_uuid(tenant_id),
            deal_id=deal_id,
            actor_id=actor_id,
            to=body.to,
            reason=body.reason,
            expected_revision=body.expected_revision,
            audit_metadata={
                **(audit_extra or {}),
                "acknowledged_outstanding_item_ids": body.acknowledged_outstanding_item_ids,
                "acknowledged_contradiction_ids": body.acknowledged_contradiction_ids,
            }
            if body.to == "APPROVED"
            else audit_extra,
            acknowledged_outstanding_item_ids=body.acknowledged_outstanding_item_ids,
            acknowledged_contradiction_ids=body.acknowledged_contradiction_ids,
        )
    except DealNotFound:
        raise _http_error("DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None
    except DealInvalidTransition as exc:
        raise _http_error(
            "DEAL_INVALID_TRANSITION",
            f"cannot transition {exc.from_state} -> {exc.to_state}",
            409,
        ) from None
    except DealRevisionMismatch as exc:
        raise _http_error(
            "DEAL_REVISION_MISMATCH",
            str(exc),
            409,
        ) from None
    except ValueError as exc:
        raise _http_error("APPROVAL_ACKNOWLEDGEMENT_INVALID", str(exc), 422) from None


def _approval_audit_metadata(tenant_id: str, deal_id: str) -> dict | None:
    """The theme version and template reference an approval is recorded against."""
    from creditpaper.deals.routes.paper import deal_logo_path
    from creditpaper.deals.services.deal_templates import effective_template, template_ref
    from creditpaper.deals.store import deal_template_store
    from creditpaper.paper.theme import get_theme

    try:
        deal = deal_template_store.get_deal(tenant_id, deal_id)
        if deal is None:
            return None  # the transition itself answers 404
        template_id, source, snapshot = effective_template(tenant_id, deal)
        theme = get_theme(tenant_id, logo_endpoint=deal_logo_path(deal_id))
        return {
            "theme_version": theme.get("version"),
            "template": {**template_ref(template_id, snapshot), "source": source},
        }
    except Exception:  # noqa: BLE001 — recorded as unavailable rather than blocking approval
        log.warning("approval audit: theme/template reference unavailable for deal %s", deal_id)
        return {"theme_version": None, "template": None}


@router.get("/{deal_id}/transitions")
def list_transitions_route(
    deal_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
):
    rows = list_transitions(tenant_id, deal_id)
    if rows is None:
        raise _http_error("DEAL_NOT_FOUND", f"deal {deal_id} not found", 404)
    return rows


@router.get("/{deal_id}/audit")
def list_audit_route(
    deal_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
):
    rows = list_audit(tenant_id, deal_id)
    if rows is None:
        raise _http_error("DEAL_NOT_FOUND", f"deal {deal_id} not found", 404)
    return rows


@router.get("/{deal_id}/extraction-status")
def get_extraction_status(
    deal_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
):
    snapshot = extraction_status_snapshot(tenant_id, deal_id)
    if snapshot is None:
        raise _http_error("DEAL_NOT_FOUND", f"deal {deal_id} not found", 404)
    return snapshot


@router.post("/{deal_id}/processing/resume", status_code=202)
def resume_processing(
    deal_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
    _actor: User = _workspace_writer,
):
    """Redeliver durable pending work after a worker or broker interruption."""
    if not workspace_exists(tenant_id, deal_id):
        raise _http_error("DEAL_NOT_FOUND", f"deal {deal_id} not found", 404)
    from creditpaper.queueing.dispatch import resume_deal

    result = resume_deal(tenant_id, deal_id)
    if not result["worker_alive"]:
        raise _http_error(
            "PROCESSING_WORKER_OFFLINE",
            "The processing service is still offline. Your files are safe; try again shortly.",
            503,
        )
    return {**result, "status": "resumed"}


@router.post("/{deal_id}/extract", status_code=202)
def trigger_extraction(
    deal_id: str,
    background_tasks: BackgroundTasks,
    engine: str = Query("registry", pattern="^(registry|corpus)$"),
    passes: int = Query(0, ge=0, le=5),
    tenant_id: str = Depends(resolve_tenant_id),
    _actor: User = _workspace_writer,
):
    if not workspace_exists(tenant_id, deal_id):
        raise _http_error("DEAL_NOT_FOUND", f"deal {deal_id} not found", 404)

    from creditpaper.ledger import jobs

    kind = "corpus_extraction" if engine == "corpus" else "extraction"
    payload = {"passes": passes} if engine == "corpus" and passes else {}
    job_id = jobs.enqueue(tenant_id, deal_id, kind, "deal", payload)
    jobs.dispatch(tenant_id, job_id)
    return {"job_id": job_id, "status": "started", "engine": engine}


@router.post("/{deal_id}/documents")
def upload_document(
    deal_id: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),  # noqa: B008
    display_name: str | None = Form(default=None),
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _workspace_writer,
):
    actor_id = str(user.id)
    try:
        from creditpaper.deals.services.workspace import MAX_UPLOAD_BYTES

        file_bytes = file.file.read(MAX_UPLOAD_BYTES + 1)
        result = upload_document_service(
            tenant_id=tenant_uuid(tenant_id),
            deal_id=deal_id,
            actor_id=actor_id,
            file_bytes=file_bytes,
            original_filename=file.filename or "upload.bin",
            mime_type=file.content_type or "application/octet-stream",
            display_name=display_name,
        )
    except DealNotFound:
        raise _http_error("DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None
    except DealDocumentEmpty:
        raise _http_error("DEAL_DOCUMENT_EMPTY", "file is empty", 400) from None
    except DealDocumentTypeUnsupported as exc:
        raise _http_error("DEAL_DOCUMENT_TYPE_UNSUPPORTED", str(exc), 415) from None
    except DealDocumentTooLarge as exc:
        raise _http_error("DEAL_DOCUMENT_TOO_LARGE", str(exc), 413) from None
    except DealDocumentDuplicate as exc:
        raise _http_error("DEAL_DOCUMENT_DUPLICATE", str(exc), 409) from None

    # The upload routine durably enqueued parsing in the same transaction as
    # the version. Local development still needs an in-process worker nudge;
    # hosted environments use the separately supervised worker.
    from creditpaper.ledger import jobs

    jobs.dispatch(str(tenant_uuid(tenant_id)), result["job_id"])

    return Response(status_code=201, content=json.dumps(result), media_type="application/json")


@router.get("/{deal_id}/documents/{document_id}/versions/{version_id}/download")
def download_document(
    deal_id: str,
    document_id: str,
    version_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
):
    try:
        file_bytes, original_filename, mime_type = stream_version(
            tenant_id=tenant_uuid(tenant_id),
            deal_id=deal_id,
            document_id=document_id,
            version_id=version_id,
        )
    except DealNotFound:
        raise _http_error("DEAL_NOT_FOUND", f"document version {version_id} not found", 404) from None

    return Response(
        content=file_bytes,
        media_type=mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{original_filename}"',
            "Content-Length": str(len(file_bytes)),
        },
    )


# --- Document deletion ---


@router.delete("/{deal_id}/documents/{document_id}", status_code=200)
def delete_document(
    deal_id: str,
    document_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _workspace_writer,
):
    """Archive a source and retract its current facts while retaining history."""
    from creditpaper.ledger.service import archive_document

    archive_document(tenant_id, deal_id, document_id, str(user.id))
    return {"ok": True, "deleted": document_id}


# --- Per-document extraction and facts ---


@router.post("/{deal_id}/documents/{document_id}/extract", status_code=202)
def trigger_document_extract(
    deal_id: str,
    document_id: str,
    background: BackgroundTasks,
    tenant_id: str = Depends(resolve_tenant_id),
    _actor: User = _workspace_writer,
):
    """Re-extract facts for a single document (retry on failure)."""
    version_id = latest_document_version_id(tenant_id, deal_id, document_id)
    if version_id is None:
        raise _http_error("DOCUMENT_NOT_FOUND", f"document {document_id} not found", 404)
    job_id = _process_version_safe(
        version_id,
        deal_id=deal_id,
        tenant_id=tenant_uuid(tenant_id),
    )
    return {"status": "queued", "version_id": version_id, "job_id": job_id}


@router.get("/{deal_id}/documents/{document_id}/facts")
def get_document_facts(
    deal_id: str,
    document_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
):
    """Return current facts anchored to this exact document."""
    from creditpaper.ledger.service import load_model

    model = load_model(tenant_id, deal_id)
    facts = _flatten_facts(model.model_dump(mode="json")) if model else []
    selected = [f for f in facts if f.get("document_id") == document_id]
    return {"facts": selected, "total": len(selected)}


# --- Fact Review Queue endpoints ---


@router.get("/{deal_id}/reviews")
def list_reviews(
    deal_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
):
    """Get pending review items for a deal."""
    require_ready(tenant_id, deal_id, capability="facts")
    from creditpaper.review.service import get_pending_reviews

    return get_pending_reviews(tenant_id=tenant_uuid(tenant_id), deal_id=deal_id)


@router.get("/{deal_id}/reviews/stats")
def review_stats(
    deal_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
):
    """Get review queue statistics."""
    from creditpaper.review.service import get_review_stats

    return get_review_stats(tenant_id=tenant_uuid(tenant_id), deal_id=deal_id)


class ApproveReviewRequest(BaseModel):
    reviewed_value: str | None = None
    notes: str = Field(default="Reviewed by analyst", min_length=1, max_length=2000)
    expected_revision: int = Field(ge=0)


@router.post("/{deal_id}/reviews/{queue_id}/approve")
def approve_review(
    deal_id: str,
    queue_id: str,
    body: ApproveReviewRequest,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _workspace_reviewer,
):
    """Approve or correct a fact in the review queue."""
    from creditpaper.review.service import approve_fact

    return approve_fact(
        queue_id=queue_id,
        reviewer_id=str(user.id),
        reviewed_value=body.reviewed_value,
        notes=body.notes,
        tenant_id=tenant_id,
        deal_id=deal_id,
        expected_revision=body.expected_revision,
    )


@router.post("/{deal_id}/reviews/{queue_id}/reject")
def reject_review(
    deal_id: str,
    queue_id: str,
    body: ApproveReviewRequest,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _workspace_reviewer,
):
    """Reject a fact — it was incorrectly extracted."""
    from creditpaper.review.service import reject_fact

    return reject_fact(
        queue_id=queue_id,
        reviewer_id=str(user.id),
        notes=body.notes,
        tenant_id=tenant_id,
        deal_id=deal_id,
        expected_revision=body.expected_revision,
    )


# ---------------------------------------------------------------------------
# Credit paper generation
# ---------------------------------------------------------------------------


class GenerateRequest(BaseModel):
    sections: list[int] | None = None


@router.post("/{deal_id}/paper/generate")
def generate_paper(
    deal_id: str,
    body: GenerateRequest | None = None,
    tenant_id: str = Depends(resolve_tenant_id),
    actor: User = _workspace_writer,
):
    """Start credit paper generation for a deal."""
    require_ready(tenant_id, deal_id)
    from creditpaper.deals.services.deal_templates import DealNotFound as TemplateDealNotFound
    from creditpaper.deals.services.deal_templates import ensure_snapshot
    from creditpaper.deals.services.generation import start_generation

    # First generation fixes the deal's template: capture the snapshot it was shown with.
    try:
        ensure_snapshot(tenant_id, deal_id)
    except TemplateDealNotFound:
        raise _http_error("DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None

    sections = body.sections if body else None
    result = start_generation(deal_id=deal_id, tenant_id=tenant_id, sections=sections, actor_id=str(actor.id))
    if isinstance(result, dict) and "error" in result:
        raise _http_error("GENERATION_IN_PROGRESS", result["error"], 409)
    return {"job_id": result, "status": "running", "sections_queued": len(sections) if sections else 9}


@router.get("/{deal_id}/paper/generate/status")
def generate_status(
    deal_id: str,
    job_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
):
    """Get generation job status."""
    from creditpaper.deals.services.deal_templates import DealNotFound
    from creditpaper.deals.services.generation import GenerationJobNotFound, get_generation_status
    from creditpaper.deals.services.paper_sections import job_status_by_key

    try:
        job = get_generation_status(deal_id=deal_id, job_id=job_id, tenant_id=tenant_id)
    except GenerationJobNotFound as exc:
        raise _http_error("GENERATION_JOB_NOT_FOUND", str(exc), 404) from None
    # Sections are reported by template section key, the same keys GET /paper/sections uses.
    try:
        return job_status_by_key(tenant_id, deal_id, job)
    except DealNotFound:
        raise _http_error(
            "GENERATION_JOB_NOT_FOUND", f"Job {job_id} not found for deal {deal_id}", 404
        ) from None


@router.post("/{deal_id}/paper/generate/{section}")
def regenerate_section(
    deal_id: str,
    section: str,
    tenant_id: str = Depends(resolve_tenant_id),
    actor: User = _workspace_writer,
):
    """Regenerate a single section.

    `section` is the template section `key` from GET /paper/sections; an engine section
    number (digits) is still accepted for backwards compatibility.
    """
    require_ready(tenant_id, deal_id)
    from creditpaper.deals.services.deal_templates import DealNotFound as TemplateDealNotFound
    from creditpaper.deals.services.generation import regenerate_section as do_regenerate
    from creditpaper.deals.services.paper_sections import (
        SectionNotFound,
        SectionNotWritable,
        engine_section_for,
    )

    try:
        engine_number, key = engine_section_for(tenant_id, deal_id, section)
    except TemplateDealNotFound:
        raise _http_error("DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None
    except SectionNotFound:
        raise _http_error("INVALID_SECTION", f"no section '{section}' in this deal's template", 400) from None
    except SectionNotWritable:
        raise _http_error("SECTION_NOT_WRITABLE", f"section '{section}' cannot be written yet", 409) from None
    try:
        result = do_regenerate(
            deal_id=deal_id, tenant_id=tenant_id, section_num=engine_number, actor_id=str(actor.id)
        )
    except ValueError as exc:
        raise _http_error("INVALID_SECTION", str(exc), 400) from None
    # `section` echoes what was asked for: the key, or the number from an older caller.
    return {**result, "section": key if key is not None else engine_number, "key": key}


@router.get("/{deal_id}/paper/sections")
def get_sections(
    deal_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
):
    """The deal's paper in its template's order, joined with generated sections."""
    from creditpaper.deals.services.deal_templates import DealNotFound as TemplateDealNotFound
    from creditpaper.deals.services.paper_sections import get_paper_sections

    try:
        return get_paper_sections(tenant_id, deal_id)
    except TemplateDealNotFound:
        raise _http_error("DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None


# ---------------------------------------------------------------------------
# Slot board and fact sheet (contract 2026-09-15 §A, §B). Included here so they share
# the `/api/deals` prefix without a new import in `api/main.py`.
# ---------------------------------------------------------------------------

from creditpaper.deals.routes.activity import router as _activity_router  # noqa: E402
from creditpaper.deals.routes.fact_sheet import router as _fact_sheet_router  # noqa: E402
from creditpaper.deals.routes.recommendation import router as _recommendation_router  # noqa: E402
from creditpaper.deals.routes.slots import router as _slots_router  # noqa: E402

router.include_router(_slots_router)
router.include_router(_fact_sheet_router)
router.include_router(_activity_router)
router.include_router(_recommendation_router)
