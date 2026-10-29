"""Read-only accessors for the superadmin credit-ops stored procedures.

Every statement in this module is a bare `SELECT public.<fn>(...)` call - the
no-inline-SQL ratchet (tests/test_no_new_inline_sql.py) allows exactly that
shape and nothing else. SQL is passed to `_one` as a parameter so each wrapper
keeps its literal visible at the call site.
"""

from __future__ import annotations

import json
from typing import Any

from creditpaper.db import read_cursor


def _one(sql: str, params: tuple[Any, ...] = ()) -> Any:
    with read_cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    value = row[0]
    return json.loads(value) if isinstance(value, str) else value


def get_sa_overview() -> dict[str, Any]:
    payload = _one("SELECT public.get_sa_overview()")
    return payload or {}


def get_sa_deals_list(
    *,
    search: str = "",
    state: str = "",
    tenant_id: str | None = None,
    deal_type: str = "",
    date_from: str | None = None,
    date_to: str | None = None,
    order_by: str = "",
    page: int = 1,
    limit: int = 20,
) -> dict[str, Any]:
    # page/limit carry explicit int casts: psycopg sends a Python int as
    # smallint, which would not resolve against an INTEGER parameter.
    payload = _one(
        "SELECT public.get_sa_deals_list(%s,%s,%s,%s,%s,%s,%s,%s::int,%s::int)",
        (
            search or None,
            state or None,
            tenant_id or None,
            deal_type or None,
            date_from or None,
            date_to or None,
            order_by or None,
            page,
            limit,
        ),
    )
    return payload or {}


def get_sa_deal_ref(deal_id: str) -> dict[str, Any] | None:
    payload = _one("SELECT public.get_sa_deal_ref(%s)", (deal_id,))
    return payload if isinstance(payload, dict) else None


def get_sa_deal_workspace(deal_id: str) -> dict[str, Any] | None:
    payload = _one("SELECT public.get_sa_deal_workspace(%s)", (deal_id,))
    return payload if isinstance(payload, dict) else None


def get_sa_deal_documents(deal_id: str) -> dict[str, Any] | None:
    payload = _one("SELECT public.get_sa_deal_documents(%s)", (deal_id,))
    return payload if isinstance(payload, dict) else None


def get_sa_fact_sheet(deal_id: str) -> dict[str, Any] | None:
    payload = _one("SELECT public.get_sa_fact_sheet(%s)", (deal_id,))
    return payload if isinstance(payload, dict) else None


def get_sa_validation(deal_id: str) -> dict[str, Any] | None:
    payload = _one("SELECT public.get_sa_validation(%s)", (deal_id,))
    return payload if isinstance(payload, dict) else None


def get_sa_paper(deal_id: str) -> dict[str, Any] | None:
    payload = _one("SELECT public.get_sa_paper(%s)", (deal_id,))
    return payload if isinstance(payload, dict) else None


def get_sa_queue(tenant_id: str | None = None, page: int = 1, limit: int = 20) -> dict[str, Any]:
    payload = _one("SELECT public.get_sa_queue(%s,%s::int,%s::int)", (tenant_id or None, page, limit))
    return payload or {}


def get_sa_template_usage() -> list[dict[str, Any]]:
    payload = _one("SELECT public.get_sa_template_usage()")
    return payload if isinstance(payload, list) else []
