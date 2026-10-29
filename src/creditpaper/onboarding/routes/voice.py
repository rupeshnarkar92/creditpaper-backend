from __future__ import annotations

import threading
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import CurrentUser, require_permission
from creditpaper.onboarding.services.voice_service import (
    generate_voice_profile,
    get_voice_profile,
)
from creditpaper.onboarding.utils.store import (
    validated_count,
)

router = APIRouter(prefix="/api/onboarding/voice", tags=["onboarding"])

# Generation status lives in the voice_generation_state table (SPs only — no
# inline SQL), so it survives restarts and is correct across workers.
# Status literals: "generating" | "done" | "error".
_STALE_AFTER_SECONDS = 15 * 60


def _set_generation_state(tenant_id: str, status: str, job_id: str, error: str | None = None) -> None:
    from creditpaper.db import tenant_cursor

    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.set_voice_generation_state(%s, %s, %s, %s)",
            (tenant_id, status, job_id, error),
        )


def _claim_generation(tenant_id: str, job_id: str) -> bool:
    from creditpaper.db import tenant_cursor

    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT public.claim_voice_generation_job(%s, %s)", (tenant_id, job_id))
        row = cur.fetchone()
    return bool(row[0]) if row else False


def _get_generation_state(tenant_id: str) -> dict[str, Any]:
    import json as _json

    from creditpaper.db import tenant_cursor

    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT public.get_voice_generation_state(%s)", (tenant_id,))
        row = cur.fetchone()
    value = row[0] if row else None
    if isinstance(value, str):
        value = _json.loads(value)
    return dict(value) if isinstance(value, dict) else {}


def _state_is_stale(state: dict[str, Any]) -> bool:
    from datetime import UTC, datetime

    try:
        updated = datetime.fromisoformat(str(state.get("updated_at")).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return True
    return (datetime.now(UTC) - updated).total_seconds() > _STALE_AFTER_SECONDS


def _run_generation(tenant_id: str, job_id: str) -> None:
    """Background thread that performs LLM voice extraction."""
    from creditpaper.llm.tenant_config import provider_session

    try:
        with provider_session(tenant_id):
            generate_voice_profile(tenant_id)
        _set_generation_state(tenant_id, "done", job_id)
    except Exception as exc:
        _set_generation_state(tenant_id, "error", job_id, str(exc))


@router.post("/generate", response_model=dict)
def post_generate(
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _admin: CurrentUser = Depends(require_permission("settings.update")),  # noqa: B008
):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())

    # Already generating? (Stale rows from a crashed worker are claimable again.)
    current = _get_generation_state(tenant_id)
    if current.get("status") == "generating" and not _state_is_stale(current):
        return {"job_id": current.get("job_id"), "status": "generating"}

    # Validate prerequisites synchronously (fast checks)
    count = validated_count(tenant_id)
    if count < 1:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "NEED_VALIDATED_PAPER",
                    "message": "need a validated paper",
                    "request_id": request_id,
                }
            },
        )

    # Start background generation; the claim is atomic so only one worker wins.
    job_id = str(uuid.uuid4())
    if not _claim_generation(tenant_id, job_id):
        current = _get_generation_state(tenant_id)
        return {"job_id": current.get("job_id"), "status": "generating"}

    thread = threading.Thread(target=_run_generation, args=(tenant_id, job_id), daemon=True)
    thread.start()

    return {"job_id": job_id, "status": "generating"}


@router.get("", response_model=dict)
def get_voice(
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _perm: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    profile = get_voice_profile(tenant_id)
    if not profile:
        raise HTTPException(  # noqa: B904
            status_code=404,
            detail={
                "error": {
                    "code": "VOICE_NOT_FOUND",
                    "message": "no voice profile",
                    "request_id": request.headers.get("x-request-id", ""),
                }
            },
        )
    return profile


@router.get("/status", response_model=dict)
def get_voice_status(
    tenant_id: str = Depends(resolve_tenant_id),
    _perm: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    import json as _json

    from creditpaper.db import cursor, tenant_uuid

    tid = tenant_uuid(tenant_id)

    # Single cursor: fetch voice profile + all validated documents in one round-trip.
    with cursor() as cur:
        cur.execute(
            """
            SELECT
              vp.profile,
              od.id, od.filename, od.status, od.approval_detected,
              od.extracted_sections, od.updated_at
            FROM (SELECT profile FROM voice_profile WHERE tenant_id = %s) vp
            CROSS JOIN LATERAL (
              SELECT id, filename, status, approval_detected,
                     extracted_sections, updated_at
              FROM onboarding_documents
              WHERE tenant_id = %s AND status = 'validated' AND approval_detected = TRUE
              ORDER BY created_at
            ) od
            """,
            (tid, tid),
        )
        rows = cur.fetchall()

    # ── voice profile (same for every row, or absent) ──────────────────────
    profile = None
    docs: list[dict] = []
    seen_ids: set[str] = set()
    for row in rows:
        if profile is None and row[0] is not None:
            profile = row[0]
        doc_id = str(row[1])
        if doc_id not in seen_ids:
            seen_ids.add(doc_id)
            secs = row[5] or []
            if isinstance(secs, str):
                try:
                    secs = _json.loads(secs)
                except Exception:
                    secs = []
            docs.append(
                {
                    "id": doc_id,
                    "filename": row[2],
                    "status": row[3],
                    "approval_detected": row[4],
                    "extracted_sections": secs,
                    "updated_at": row[6].isoformat() if row[6] else None,
                }
            )

    count = len(docs)

    # ── corpus preview (first doc with substantial body text) ──────────────
    corpus_preview = None
    for d in docs:
        for section in d.get("extracted_sections") or []:
            body = (section.get("preview") or section.get("body") or "").strip()
            if body and len(body) > 50:
                corpus_preview = {
                    "filename": d["filename"],
                    "section_name": section.get("name", "Extracted Text"),
                    "text": body[:3000],
                }
                break
        if corpus_preview:
            break

    # ── generation state (voice_generation_state table) ────────────────────
    gen_state = _get_generation_state(tenant_id)
    generating = gen_state.get("status") == "generating" and not _state_is_stale(gen_state)
    gen_error = gen_state.get("error") if gen_state.get("status") == "error" else None

    # ── stale detection ───────────────────────────────────────────────────
    stale_reason = None
    if profile and docs:
        sources = {s.get("document_id") for s in profile.get("sources", [])}
        current = {d.get("id") for d in docs}
        if sources - current:
            stale_reason = "source_deleted"
        elif sources != current:
            stale_reason = "papers_changed"
        else:
            from datetime import datetime

            def timestamp(value):
                try:
                    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    return 0

            if any(timestamp(d.get("updated_at")) > timestamp(profile.get("updated_at")) for d in docs):
                stale_reason = "papers_changed"

    return {
        "has_voice": profile is not None,
        "voice": profile,
        "validated_count": count,
        "can_generate": count >= 1,
        "generating": generating,
        "generation_error": gen_error,
        "corpus_preview": corpus_preview,
        "stale": stale_reason is not None,
        "stale_reason": stale_reason,
    }
