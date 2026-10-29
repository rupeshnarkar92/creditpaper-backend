"""Raw psycopg queries for a deal's template, paper-section rows and audit entries.

Every query that reads or writes a deal is scoped by tenant. Services call these;
tests monkeypatch them to stay DB-free.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from creditpaper.db import tenant_cursor, tenant_uuid
from creditpaper.deals.services.activity import record_deal_event


def get_deal(tenant_id: str, deal_id: str) -> dict[str, Any] | None:
    """The deal's template columns, or None if the deal is not in this tenant."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.deal_template_row(%s,%s)", (tenant_uuid(tenant_id), deal_id))
        row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "deal_type": row[1],
        "template_id": str(row[2]) if row[2] else None,
        "template_source": row[3],
        "template_snapshot": row[4],
        "has_paper_sections": bool(row[5]),
    }


def list_paper_section_rows(tenant_id: str, deal_id: str) -> list[dict[str, Any]]:
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.deal_paper_section_rows(%s)", (deal_id,))
        rows = cur.fetchall()
    return [
        {
            "section": int(r[0]),
            "status": r[1],
            "prose": r[2],
            "citations": r[3] or [],
            "error": r[4],
            "editor_value": r[5],
            "edit_revision": r[6],
        }
        for r in rows
    ]


def active_generation(tenant_id: str, deal_id: str) -> dict | None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.active_generation_job(%s,%s)", (tenant_uuid(tenant_id), deal_id))
        row = cur.fetchone()
    return {"job_id": str(row[0]), "status": row[1]} if row else None


def _audit(
    cur: Any, *, tenant_id: str, deal_id: str, actor_id: str | None, action: str, metadata: dict
) -> None:
    record_deal_event(
        cur,
        tenant_id=tenant_uuid(tenant_id),
        deal_id=deal_id,
        actor_id=actor_id,
        action=action,
        entity_type="deal",
        entity_id=deal_id,
        metadata=metadata,
        created_at=datetime.now(UTC),
    )


def insert_audit_event(
    *, tenant_id: str, deal_id: str, actor_id: str | None, action: str, metadata: dict[str, Any]
) -> None:
    with tenant_cursor(tenant_id) as cur:
        _audit(cur, tenant_id=tenant_id, deal_id=deal_id, actor_id=actor_id, action=action, metadata=metadata)


def change_template(
    *,
    tenant_id: str,
    deal_id: str,
    template_id: str | None,
    template_source: str,
    snapshot: dict[str, Any],
    actor_id: str | None,
    audit_metadata: dict[str, Any],
) -> str:
    """Replace the deal's template unless first generation has happened.

    One transaction: the lock check is part of the UPDATE's WHERE clause, so a
    generation that lands between the read and the write cannot be overwritten.
    Returns "ok", "locked" or "not_found".
    """
    with tenant_cursor(tenant_id) as cur:
        with cur.connection.transaction():
            # Generation enqueue takes the same deal lock. Read the active-job predicate
            # in a fresh statement after obtaining it, so a concurrently queued job is visible.
            cur.execute(
                "SELECT public.change_deal_template(%s,%s,%s,%s,%s::jsonb)",
                (tenant_uuid(tenant_id), deal_id, template_id, template_source, json.dumps(snapshot)),
            )
            outcome = cur.fetchone()[0]
            if outcome != "ok":
                return outcome
            _audit(
                cur,
                tenant_id=tenant_id,
                deal_id=deal_id,
                actor_id=actor_id,
                action="template.changed",
                metadata=audit_metadata,
            )
    return "ok"


def write_snapshot_if_absent(
    *,
    tenant_id: str,
    deal_id: str,
    template_id: str | None,
    template_source: str,
    snapshot: dict[str, Any],
) -> bool:
    """Capture the snapshot on first generation. Never overwrites an existing one."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.write_template_snapshot_if_absent(%s,%s,%s,%s,%s::jsonb)",
            (tenant_uuid(tenant_id), deal_id, template_id, template_source, json.dumps(snapshot)),
        )
        return bool(cur.fetchone()[0])
