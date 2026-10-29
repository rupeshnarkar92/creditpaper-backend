"""Tenant-scoped deals lists, including slot counts, in one database call."""

from math import ceil
from functools import cmp_to_key
from typing import Any

from creditpaper.db import tenant_cursor, tenant_uuid


def list_deals(tenant_id: str) -> list[dict[str, Any]]:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.get_deals_list(%s)", (tid,))
        return cur.fetchone()[0]


_SORT_FIELDS = {
    "name": "name",
    "borrower": "borrower",
    "facility_amount": "facility_amount",
    "deal_type": "deal_type",
    "created_at": "created_at",
    "updated_at": "updated_at",
    "current_state": "current_state",
}


def _status(row: dict[str, Any]) -> str:
    state = str(row.get("current_state") or "").upper()
    if state in {"DRAFT", "INTAKE"}:
        return "draft"
    if state in {"APPROVED", "ISSUED", "ARCHIVED"}:
        return "approved"
    if state == "REJECTED":
        return "needs_attention"
    return "in_review"


def list_deals_page(
    tenant_id: str,
    *,
    page: int = 1,
    limit: int = 10,
    search: str = "",
    stage: str = "",
    sort_by: str = "updated_at",
    sort_dir: str = "desc",
) -> dict[str, Any]:
    """Return the standard server-side list envelope for the Deals page.

    The stored read remains the single source for tenant isolation and document
    counts. Filtering and ordering are deliberately whitelisted here so the API
    can expose a stable list contract before moving the work into SQL.
    """
    rows = list_deals(tenant_id)
    needle = search.strip().casefold()
    if needle:
        rows = [
            row for row in rows
            if needle in str(row.get("name") or "").casefold()
            or needle in str(row.get("borrower") or "").casefold()
        ]
    if stage:
        rows = [row for row in rows if _status(row) == stage]

    field = _SORT_FIELDS.get(sort_by, "updated_at")
    descending = sort_dir == "desc"
    def compare(left: dict[str, Any], right: dict[str, Any]) -> int:
        left_value = left.get(field)
        right_value = right.get(field)
        if left_value is None and right_value is not None:
            return 1
        if left_value is not None and right_value is None:
            return -1
        if left_value != right_value:
            if isinstance(left_value, (int, float)) and isinstance(right_value, (int, float)):
                result = -1 if left_value < right_value else 1
            else:
                left_text = str(left_value or "").casefold()
                right_text = str(right_value or "").casefold()
                result = -1 if left_text < right_text else 1
            return -result if descending else result
        left_id = str(left.get("id") or "")
        right_id = str(right.get("id") or "")
        return -1 if left_id < right_id else 1 if left_id > right_id else 0

    rows.sort(key=cmp_to_key(compare))

    total = len(rows)
    safe_limit = max(1, min(limit, 100))
    pages = max(1, ceil(total / safe_limit))
    safe_page = min(max(page, 1), pages)
    start = (safe_page - 1) * safe_limit
    return {
        "rows": rows[start:start + safe_limit],
        "total": total,
        "page": safe_page,
        "limit": safe_limit,
        "pages": pages,
    }
