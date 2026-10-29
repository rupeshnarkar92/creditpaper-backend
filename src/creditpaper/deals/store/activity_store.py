"""Raw psycopg reads for a deal's Activity. Every query is scoped by tenant.

The service calls these; tests replace them with an in-memory fake. Writes go through
`deals/services/activity.py::record_deal_event`, never here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from creditpaper.db import tenant_cursor, tenant_uuid


def get_deal(tenant_id: str, deal_id: str) -> dict[str, Any] | None:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT * FROM public.deal_activity_identity(%s, %s)", (tid, deal_id))
        row = cur.fetchone()
    return {"id": row[0], "name": row[1], "created_at": row[2]} if row else None


def count_by_action(tenant_id: str, deal_id: str) -> dict[str, int]:
    """Stored events per action slug. A deal has a handful of distinct slugs, so the
    category counts and the category filter are derived from this in the service."""
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT * FROM public.deal_activity_counts(%s, %s)", (tid, deal_id))
        rows = cur.fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def list_events(
    tenant_id: str,
    deal_id: str,
    *,
    actions: list[str] | None,
    before: tuple[datetime, str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Newest first, keyset on (created_at, id). Index: ix_deal_audit_events_activity (0016)."""
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT * FROM public.deal_activity_page(%s, %s, %s, %s, %s, %s)",
            (tid, deal_id, actions, before[0] if before else None, before[1] if before else None, limit),
        )
        rows = cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "actor_id": str(r[1]) if r[1] else None,
            "action": r[2],
            "entity_type": r[3],
            "entity_id": r[4],
            "metadata": r[5] if isinstance(r[5], dict) else {},
            "created_at": r[6],
        }
        for r in rows
    ]


def actor_names(tenant_id: str, actor_ids: list[str]) -> dict[str, str]:
    """One query for the page: display names of the members of this tenant among `actor_ids`.
    Ids that are not a member (the tenant placeholder, system ids) are simply absent."""
    if not actor_ids:
        return {}
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT * FROM public.tenant_actor_names(%s, %s)", (tid, actor_ids))
        rows = cur.fetchall()
    out: dict[str, str] = {}
    for user_id, first, last, email in rows:
        name = " ".join(p for p in ((first or "").strip(), (last or "").strip()) if p)
        if not name and email:
            name = str(email).split("@", 1)[0]
        if name:
            out[str(user_id)] = name
    return out


# --- tenant-wide (Home) ----------------------------------------------------------------


def count_tenant_by_action(tenant_id: str) -> dict[str, int]:
    """Stored events per action slug across the tenant's deals."""
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT * FROM public.tenant_activity_counts(%s)", (tid,))
        rows = cur.fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def list_tenant_events(
    tenant_id: str,
    *,
    actions: list[str] | None,
    before: tuple[datetime, str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Newest first across the tenant, keyset on (created_at, id), each with its deal's name."""
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT * FROM public.tenant_activity_page(%s, %s, %s, %s, %s)",
            (tid, actions, before[0] if before else None, before[1] if before else None, limit),
        )
        rows = cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "actor_id": str(r[1]) if r[1] else None,
            "action": r[2],
            "entity_type": r[3],
            "entity_id": r[4],
            "metadata": r[5] if isinstance(r[5], dict) else {},
            "created_at": r[6],
            "deal": {"id": str(r[7]), "name": r[8]},
        }
        for r in rows
    ]


def call_tenant_activity(
    tenant_id: str, *, category: str | None, limit: int, before: tuple[datetime, str] | None
) -> dict[str, Any]:
    """Counts, page and actor names in one statement; filtering precedes pagination."""
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT public.get_tenant_activity(%s, %s, %s, %s, %s)",
            (
                tid,
                category,
                limit,
                before[0] if before else None,
                before[1] if before else None,
            ),
        )
        return cur.fetchone()[0]
