"""Raw psycopg queries for billing. Same pattern as guardrails/store: shared pool, no ORM.

Every query is keyed by tenant. The tenant always comes from the signed-in user's firm in the
route, never from a request header or parameter.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from creditpaper.db import cursor, tenant_cursor, tenant_uuid

_ACCOUNT_KEYS = [
    "tenant_id",
    "tier",
    "billing_interval",
    "status",
    "current_period_start",
    "current_period_end",
    "stripe_customer_id",
    "stripe_subscription_id",
    "card_brand",
    "card_last4",
    "card_exp_month",
    "card_exp_year",
    "created_at",
    "updated_at",
]


def _t(tenant_id: str) -> str:
    return str(tenant_uuid(tenant_id))


def _account(row: Any) -> dict[str, Any]:
    out = dict(zip(_ACCOUNT_KEYS, row, strict=True))
    out["tenant_id"] = str(out["tenant_id"])
    return out


def get_or_create_account(tenant_id: str) -> dict[str, Any]:
    """A firm with no billing row yet is on a trial of Starter: the first paper is free (flow C)."""
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT * FROM public.billing_get_or_create_account(%s)", (t,))
        return _account(cur.fetchone())


def find_account_by_customer(customer_id: str) -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM public.billing_find_account_by_customer(%s)", (customer_id,))
        row = cur.fetchone()
        return _account(row) if row else None


_UPDATABLE = {
    "tier",
    "billing_interval",
    "status",
    "current_period_start",
    "current_period_end",
    "stripe_customer_id",
    "stripe_subscription_id",
    "card_brand",
    "card_last4",
    "card_exp_month",
    "card_exp_year",
}


def update_account(tenant_id: str, **fields: Any) -> dict[str, Any]:
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"not updatable: {sorted(unknown)}")
    t = _t(tenant_id)
    get_or_create_account(tenant_id)
    if not fields:
        return get_or_create_account(tenant_id)
    patch = {
        key: value.isoformat() if isinstance(value, datetime) else value for key, value in fields.items()
    }
    with tenant_cursor(t) as cur:
        cur.execute("SELECT * FROM public.billing_update_account(%s, %s::jsonb)", (t, json.dumps(patch)))
        return _account(cur.fetchone())


def count_billable_events(tenant_id: str, start: datetime, end: datetime) -> int:
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT public.billing_count_events(%s, %s, %s)", (t, start, end))
        return int(cur.fetchone()[0])


def record_billable_event(tenant_id: str, deal_id: str, metadata: dict[str, Any] | None = None) -> bool:
    """Record a paper's first approved render. Idempotent: a second approval of the same deal
    is not a second billable paper. Returns True only when a new event was written."""
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute(
            "SELECT public.billing_record_event(%s, %s, %s, %s::jsonb)",
            (str(uuid.uuid4()), t, deal_id, json.dumps(metadata) if metadata else None),
        )
        return bool(cur.fetchone()[0])


def list_invoices(tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT * FROM public.billing_list_invoices(%s, %s)", (t, limit))
        return [
            {
                "id": str(r[0]),
                "number": r[1],
                "description": r[2],
                "amount": Decimal(r[3]),
                "currency": r[4],
                "status": r[5],
                "pdf_url": r[6],
                "issued_at": r[7],
            }
            for r in cur.fetchall()
        ]


def upsert_invoice(
    tenant_id: str,
    *,
    stripe_invoice_id: str,
    number: str,
    description: str,
    amount: Decimal,
    currency: str,
    status: str,
    pdf_url: str | None,
    issued_at: datetime,
) -> None:
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute(
            "SELECT public.billing_upsert_invoice(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                str(uuid.uuid4()),
                t,
                stripe_invoice_id,
                number,
                description,
                amount,
                currency,
                status,
                pdf_url,
                issued_at,
            ),
        )
