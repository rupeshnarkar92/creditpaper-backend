from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from creditpaper.db import tenant_cursor, tenant_uuid
from creditpaper.paths import ensure_output_dir

_SAFE_TENANT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _validate_tenant(tenant_id: str) -> str:
    if not tenant_id or not (_SAFE_TENANT.match(tenant_id) or _UUID_RE.match(tenant_id)):
        raise ValueError(f"invalid tenant_id: {tenant_id!r}")
    if ".." in tenant_id or "/" in tenant_id or "\\" in tenant_id:
        raise ValueError("invalid tenant_id")
    return tenant_id


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _papers_dir(tenant_id: str) -> Path:
    _validate_tenant(tenant_id)
    return ensure_output_dir("tenants", tenant_id, "onboarding", "papers")


def papers_storage_path(tenant_id: str, document_id: str, filename: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)[:120]
    return _papers_dir(tenant_id) / document_id / safe


DEFAULT_STATE: dict[str, Any] = {
    "current_step": "brand",
    "completed_steps": [],
    "status": "not_started",
    "updated_at": None,
}


def load_onboarding_state(tenant_id: str) -> dict[str, Any]:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.onboarding_state_row(%s)", (tid,))
        row = cur.fetchone()
    if not row or row[0] is None:
        return {**DEFAULT_STATE}
    return row[0]


def save_onboarding_state(tenant_id: str, state: dict[str, Any]) -> dict[str, Any]:
    tid = tenant_uuid(tenant_id)
    state = {**state, "updated_at": _now_iso()}
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.save_onboarding_state(%s,%s::jsonb)", (tid, json.dumps(state)))
    return state


def mark_step_completed(tenant_id: str, step: str) -> dict[str, Any]:
    state = load_onboarding_state(tenant_id)
    completed = set(state.get("completed_steps", []))
    completed.add(step)
    order = ["brand", "papers", "voice", "preview", "complete"]
    next_step = None
    for s in order:
        if s not in completed:
            next_step = s
            break
    state["completed_steps"] = sorted(completed, key=lambda x: order.index(x) if x in order else 99)
    state["current_step"] = next_step or "complete"
    state["status"] = "complete" if next_step is None else "in_progress"
    return save_onboarding_state(tenant_id, state)


def load_brand_profile(tenant_id: str) -> dict[str, Any] | None:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.brand_profile_row(%s)", (tid,))
        row = cur.fetchone()
    return row[0] if row else None


def save_brand_profile(tenant_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    tid = tenant_uuid(tenant_id)
    profile = {**profile, "tenant_id": tenant_id, "updated_at": _now_iso()}
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.save_brand_profile(%s,%s::jsonb)", (tid, json.dumps(profile)))
    mark_step_completed(tenant_id, "brand")
    return profile


def load_section_order(tenant_id: str) -> list[dict[str, Any]] | None:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.section_order_row(%s)", (tid,))
        row = cur.fetchone()
    return row[0] if row else None


def save_section_order(tenant_id: str, sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.save_section_order(%s,%s::jsonb)", (tid, json.dumps(sections)))
    return sections


def load_voice_profile(tenant_id: str) -> dict[str, Any] | None:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.voice_profile_row(%s)", (tid,))
        row = cur.fetchone()
    return row[0] if row else None


def save_voice_profile(tenant_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    tid = tenant_uuid(tenant_id)
    profile = {**profile, "tenant_id": tenant_id, "updated_at": _now_iso()}
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.save_voice_profile(%s,%s::jsonb)", (tid, json.dumps(profile)))
    mark_step_completed(tenant_id, "voice")
    return profile


def delete_voice_profile(tenant_id: str) -> bool:
    """Delete the tenant's voice row. Regenerate inserts fresh afterwards —
    no stale keys survive. Returns True when a row was removed."""
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.delete_voice_profile(%s)", (tid,))
        return bool(cur.fetchone()[0])


def upsert_document(tenant_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    tid = tenant_uuid(tenant_id)
    doc_id = uuid.UUID(doc["id"]) if not isinstance(doc["id"], uuid.UUID) else doc["id"]
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT * FROM public.upsert_onboarding_document("
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s::jsonb)",
            (
                doc_id,
                tid,
                doc["filename"],
                doc["status"],
                doc["is_credit_paper"],
                doc["approval_detected"],
                doc["confidence"],
                doc.get("reason"),
                doc["size"],
                doc.get("content_type"),
                json.dumps(doc.get("tags") or []),
                doc["storage_path"],
                json.dumps(doc.get("extracted_sections") or []),
                json.dumps(doc.get("extracted_style") or {}),
            ),
        )
        row = cur.fetchone()
    doc["id"] = str(row[0])
    return doc


def delete_paper(tenant_id: str, document_id: str) -> bool:
    tid = tenant_uuid(tenant_id)
    try:
        doc_id = uuid.UUID(document_id)
    except ValueError:
        return False
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.delete_onboarding_document(%s,%s)", (tid, doc_id))
        return bool(cur.fetchone()[0])


def load_documents(tenant_id: str) -> list[dict[str, Any]]:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT * FROM public.onboarding_documents(%s)", (tid,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    out: list[dict[str, Any]] = []
    for row in rows:
        sections = row.get("extracted_sections")
        style = row.get("extracted_style")
        if isinstance(sections, str):
            try:
                sections = json.loads(sections)
            except Exception:
                sections = []
        if isinstance(style, str):
            try:
                style = json.loads(style)
            except Exception:
                style = {}
        out.append(
            {
                "id": str(row["id"]),
                "filename": row["filename"],
                "status": row["status"],
                "is_credit_paper": row["is_credit_paper"],
                "approval_detected": row["approval_detected"],
                "confidence": row["confidence"],
                "reason": row["reason"],
                "size": row["size"],
                "content_type": row["content_type"],
                "tags": row.get("tags") or [],
                "storage_path": row["storage_path"],
                "extracted_sections": sections or [],
                "extracted_style": style or {},
                "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
                "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
            }
        )
    return out


def save_documents(tenant_id: str, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.delete_all_onboarding_documents(%s)", (tid,))
        for d in docs:
            upsert_document(tenant_id, d)
    return docs


def validated_count(tenant_id: str) -> int:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.validated_onboarding_count(%s)", (tid,))
        (n,) = cur.fetchone()
    return n
