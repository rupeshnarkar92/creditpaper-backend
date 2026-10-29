"""Read queries for the Deal Workspace HTTP routes.

The route layer translates these results into HTTP responses; SQL and tenant
scoping stay here so the same reads can be reused by services and tests.
"""

from __future__ import annotations

import os
from typing import Any

from creditpaper.db import tenant_cursor, tenant_uuid


def workspace_exists(tenant_id: str, deal_id: str) -> bool:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.deal_exists(%s, %s)", (tid, deal_id))
        return bool(cur.fetchone()[0])


def version_document_id(tenant_id: str, deal_id: str, version_id: str) -> str | None:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.deal_version_document_id(%s, %s, %s)", (tid, deal_id, version_id))
        row = cur.fetchone()
    return str(row[0]) if row else None


def latest_document_version_id(tenant_id: str, deal_id: str, document_id: str) -> str | None:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT public.deal_latest_document_version_id(%s, %s, %s)",
            (tid, deal_id, document_id),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def list_transitions(tenant_id: str, deal_id: str) -> list[dict[str, Any]] | None:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.deal_exists(%s, %s)", (tid, deal_id))
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT * FROM public.deal_transition_rows(%s, %s)", (tid, deal_id))
        rows = cur.fetchall()
    return [
        {
            "id": str(row[0]),
            "from_state": row[1],
            "to_state": row[2],
            "reason": row[3],
            "actor_id": str(row[4]) if row[4] else None,
            "created_at": row[5].isoformat() if row[5] else None,
        }
        for row in rows
    ]


def list_audit(tenant_id: str, deal_id: str) -> list[dict[str, Any]] | None:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.deal_exists(%s, %s)", (tid, deal_id))
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT * FROM public.deal_audit_rows(%s, %s)", (tid, deal_id))
        rows = cur.fetchall()
    return [
        {
            "id": str(row[0]),
            "actor_id": str(row[1]) if row[1] else None,
            "action": row[2],
            "entity_type": row[3],
            "entity_id": row[4],
            "metadata": row[5] if row[5] else {},
            "created_at": row[6].isoformat() if row[6] else None,
        }
        for row in rows
    ]


def processing_health(tenant_id: str, deal_id: str) -> dict[str, Any] | None:
    """Queued document count and worker liveness in one tenant-scoped read."""
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT public.processing_health(%s, %s), public.processing_stage_health(%s, %s)",
            (tid, deal_id, tid, deal_id),
        )
        row = cur.fetchone()
    if not row[0]:
        return None
    health = {**row[0], "active_stages": row[1] or []}
    if os.getenv("PROCESSING_QUEUE_BACKEND", "database").lower() == "celery":
        try:
            from creditpaper.queueing.health import workers_alive

            health["worker_alive"] = workers_alive()
            health["worker_stale"] = bool(
                (health.get("queued", 0) or health.get("processing", 0)) and not health["worker_alive"]
            )
        except Exception:
            health["worker_alive"] = False
            health["worker_stale"] = bool(health.get("queued", 0) or health.get("processing", 0))
    return health


def extraction_status(tenant_id: str, deal_id: str) -> list[dict[str, Any]] | None:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.deal_exists(%s, %s)", (tid, deal_id))
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT * FROM public.deal_extraction_status_rows_v2(%s, %s)", (tid, deal_id))
        rows = cur.fetchall()
    return [
        {
            "document_id": str(row[0]),
            "version_id": str(row[1]),
            "processing_status": row[2],
            "extraction_status": row[3],
            "reason_code": row[4],
            "retryable": bool(row[5]),
        }
        for row in rows
    ]


def extraction_status_snapshot(tenant_id: str, deal_id: str) -> dict[str, Any] | None:
    """All polling state in one tenant-scoped database checkout and routine call."""
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.deal_extraction_status_snapshot(%s, %s)", (tid, deal_id))
        row = cur.fetchone()
    return row[0] if row and row[0] else None
