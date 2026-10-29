"""Tenant-scoped paper edit history and comment thread operations."""

from __future__ import annotations

import json
from typing import Any

from creditpaper.db import tenant_cursor, tenant_uuid


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def engine_section_number(tenant_id: str, deal_id: str, section_key: str) -> int:
    from creditpaper.deals.services.paper_sections import engine_section_for

    try:
        engine_section, _ = engine_section_for(tenant_id, deal_id, section_key)
    except Exception as exc:
        from creditpaper.deals.services.deal_templates import DealNotFound
        from creditpaper.deals.services.paper_sections import SectionNotFound, SectionNotWritable

        if isinstance(exc, DealNotFound):
            raise
        if isinstance(exc, (SectionNotFound, SectionNotWritable)):
            raise ValueError("Paper section not found") from None
        raise
    return engine_section


def list_edit_history(tenant_id: str, deal_id: str, section: int) -> list[dict[str, Any]]:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.list_paper_section_edit_history(%s, %s, %s)", (tid, deal_id, section))
        row = cur.fetchone()
    return _json_value(row[0]) if row and row[0] else []


def restore_edit(
    tenant_id: str, deal_id: str, section: int, history_id: str, expected_revision: int, actor_id: str
) -> int | None:
    tid = tenant_uuid(tenant_id)
    actor = tenant_uuid(actor_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT public.restore_paper_section_edit(%s, %s, %s, %s, %s, %s)",
            (tid, deal_id, section, history_id, expected_revision, actor),
        )
        row = cur.fetchone()
    return row[0] if row else None


def list_comment_threads(
    tenant_id: str, deal_id: str, *, include_resolved: bool = True
) -> list[dict[str, Any]]:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.get_paper_comment_threads(%s, %s, %s)", (tid, deal_id, include_resolved))
        row = cur.fetchone()
    return _json_value(row[0]) if row and row[0] else []


def add_comment(
    tenant_id: str, deal_id: str, section_key: str, actor_id: str, body: str, thread_id: str | None = None
) -> str | None:
    tid = tenant_uuid(tenant_id)
    actor = tenant_uuid(actor_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT public.add_paper_comment(%s, %s, %s, %s, %s, %s)",
            (tid, deal_id, section_key, actor, body.strip(), thread_id),
        )
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def resolve_comment(tenant_id: str, deal_id: str, thread_id: str, actor_id: str) -> bool:
    tid = tenant_uuid(tenant_id)
    actor = tenant_uuid(actor_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT public.resolve_paper_comment_thread(%s, %s, %s, %s)",
            (tid, deal_id, thread_id, actor),
        )
        row = cur.fetchone()
    return bool(row and row[0])
