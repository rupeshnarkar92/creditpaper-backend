"""Raw psycopg queries for the templates domain.

Mirrors the pattern in onboarding/utils/store.py — raw SQL via the shared
connection pool, with no SQLAlchemy.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from creditpaper.db import cursor, tenant_cursor, tenant_uuid


def _t(tenant_id: str) -> str:
    """Resolve and validate the tenant id; raises ValueError on bad input."""
    return str(tenant_uuid(tenant_id))


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------


def list_for_tenant(tenant_id: str) -> dict[str, list[dict[str, Any]]]:
    """Return {'default': row, 'custom': [rows]} for the tenant.

    The 'default' value is None if no default template exists yet (e.g.
    before onboarding completes).
    """
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT * FROM public.tenant_templates(%s)", (t,))
        rows = cur.fetchall()
    out: dict[str, Any] = {"default": None, "custom": []}
    for r in rows:
        row = _row_to_dict(r)
        if row["is_default"]:
            out["default"] = row
        else:
            out["custom"].append(row)
    return out


def get_by_id(tenant_id: str, template_id: str) -> dict[str, Any] | None:
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT * FROM public.tenant_template(%s,%s)", (t, template_id))
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def get_default(tenant_id: str) -> dict[str, Any] | None:
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT * FROM public.default_template(%s)", (t,))
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def create(
    tenant_id: str,
    *,
    name: str,
    description: str | None = None,
    paper_types: list[str] | None = None,
    sections: list[dict[str, Any]] | None = None,
    is_default: bool = False,
    source: str = "custom",
    is_active: bool = True,
) -> dict[str, Any]:
    t = _t(tenant_id)
    new_id = str(uuid.uuid4())
    with tenant_cursor(t) as cur:
        cur.execute(
            "SELECT * FROM public.create_template(%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",
            (
                new_id,
                t,
                name,
                description,
                json.dumps(paper_types or []),
                json.dumps(sections or []),
                is_default,
                is_active,
                source,
            ),
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row else {}


def update(tenant_id: str, template_id: str, **fields: Any) -> dict[str, Any] | None:
    """Partial update — only the fields passed in are changed."""
    t = _t(tenant_id)
    if not fields:
        return get_by_id(tenant_id, template_id)

    allowed = {"name", "description", "paper_types", "sections", "is_active"}
    if not fields or not (set(fields) & allowed):
        return get_by_id(tenant_id, template_id)
    values = (
        fields.get("name"),
        fields.get("description"),
        json.dumps(fields["paper_types"]) if "paper_types" in fields else None,
        json.dumps(fields["sections"]) if "sections" in fields else None,
        fields.get("is_active"),
    )

    with tenant_cursor(t) as cur:
        cur.execute(
            "SELECT * FROM public.update_template(%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)",
            (t, template_id, *values),
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def delete(tenant_id: str, template_id: str) -> bool:
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT public.delete_template(%s,%s)", (t, template_id))
        return bool(cur.fetchone()[0])


def set_active(tenant_id: str, template_id: str, is_active: bool) -> dict[str, Any] | None:
    return update(tenant_id, template_id, is_active=is_active)


# ---------------------------------------------------------------------------
# template_papers
# ---------------------------------------------------------------------------


def list_papers(tenant_id: str, template_id: str) -> list[dict[str, Any]]:
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT * FROM public.template_papers(%s,%s)", (t, template_id))
        rows = cur.fetchall()
    return [_paper_row_to_dict(r) for r in rows]


def count_papers(tenant_id: str, template_id: str) -> int:
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT public.template_paper_count(%s,%s)", (t, template_id))
        (n,) = cur.fetchone()
    return n


def add_paper(
    tenant_id: str,
    template_id: str,
    *,
    filename: str,
    storage_path: str,
    is_credit_paper: bool,
    approval_detected: bool,
    confidence: float,
    size: int,
    content_type: str | None = None,
    extracted_sections: list[dict[str, Any]] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    t = _t(tenant_id)
    new_id = str(uuid.uuid4())
    with tenant_cursor(t) as cur:
        cur.execute(
            "SELECT * FROM public.add_template_paper(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
            (
                new_id,
                template_id,
                t,
                filename,
                storage_path,
                is_credit_paper,
                approval_detected,
                confidence,
                size,
                content_type,
                json.dumps(extracted_sections or []),
                json.dumps(tags or []),
            ),
        )
        row = cur.fetchone()
    return _paper_row_to_dict(row) if row else {}


def delete_paper(tenant_id: str, template_id: str, paper_id: str) -> bool:
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT public.delete_template_paper(%s,%s,%s)", (t, template_id, paper_id))
        return bool(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# paper_types
# ---------------------------------------------------------------------------


def list_paper_types(only_active: bool = True) -> list[dict[str, Any]]:
    with cursor() as cur:
        if only_active:
            cur.execute("SELECT * FROM public.active_paper_types(TRUE)")
        else:
            cur.execute("SELECT * FROM public.active_paper_types(FALSE)")
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "label": r[1],
            "description": r[2],
            "is_active": r[3],
            "sort_order": r[4],
        }
        for r in rows
    ]


def list_all_templates(
    *,
    tenant_id: str | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
    order_by: str = "-updated_at",
) -> tuple[list[dict[str, Any]], int]:
    """Superadmin view: every tenant's templates, newest first.

    `tenant_id` is a filter, never an access check — the caller is already
    gated by require_superadmin. Raises ValueError on a non-UUID tenant id.
    """
    where = ["1=1"]
    params: list[Any] = []
    if tenant_id:
        where.append("tenant_id = %s")
        params.append(_t(tenant_id))
    if q:
        where.append("(name ILIKE %s OR COALESCE(description, '') ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    clause = " AND ".join(where)
    descending = order_by.startswith("-")
    sort_key = order_by[1:] if descending else order_by
    sort_column = {
        "name": "name",
        "created_at": "created_at",
        "updated_at": "updated_at",
        "is_default": "is_default",
        "is_active": "is_active",
    }.get(sort_key, "updated_at")
    direction = "DESC" if descending else "ASC"
    with cursor() as cur:
        cur.execute(f"SELECT count(*) FROM templates WHERE {clause}", params)
        total = int(cur.fetchone()[0])
        cur.execute(
            f"SELECT id, tenant_id, name, description, paper_types, sections,"
            f" is_default, is_active, source, created_at, updated_at"
            f" FROM templates WHERE {clause}"
            f" ORDER BY {sort_column} {direction}, id DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows], total


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a SELECT row tuple to a dict matching TemplateOut."""
    return {
        "id": str(row[0]),
        "tenant_id": str(row[1]),
        "name": row[2],
        "description": row[3],
        "paper_types": list(row[4] or []),
        "sections": list(row[5] or []),
        "is_default": bool(row[6]),
        "is_active": bool(row[7]),
        "source": row[8],
        "created_at": row[9],
        "updated_at": row[10],
    }


def _paper_row_to_dict(row: Any) -> dict[str, Any]:
    sections = row[10]
    if isinstance(sections, str):
        try:
            sections = json.loads(sections)
        except Exception:
            sections = []
    tags = row[11]
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    created = row[12]
    return {
        "id": str(row[0]),
        "template_id": str(row[1]),
        "tenant_id": str(row[2]),
        "filename": row[3],
        "storage_path": row[4],
        "is_credit_paper": bool(row[5]),
        "approval_detected": bool(row[6]),
        "confidence": float(row[7]),
        "size": int(row[8]),
        "content_type": row[9],
        "extracted_sections": list(sections or []),
        "tags": list(tags or []),
        "created_at": created,
    }
