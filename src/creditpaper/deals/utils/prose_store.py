"""Reading and listing the generated prose caches.

This lived in `api/deals.py`, and `harness/render_reviewed.py` imported it from
there — so the ENGINE imported from the HTTP edge, while `api/main.py` imported
`render_reviewed_paper` back out of `harness`. A package-level cycle in which the
engine depends on the platform.

That inversion is what makes the `contracts/` vs `engine/` split in the 2026-08-21
architecture spec impossible to perform: `engine/` cannot be lifted out while it
reaches into `api/`. Both sides now import from here, and the dependency runs one
way — api → harness, never back.

Prose is stored in the `paper_prose` Postgres table (one row per deal). The
deal id is validated at the seam so an attacker can't smuggle a path traversal
into the WHERE clause; `tenant_id` is looked up at write time so cross-tenant
overwrites are impossible.
"""

from __future__ import annotations

import json
from typing import Any

from creditpaper.db import tenant_cursor, tenant_uuid

_INVALID_DEAL = ValueError("invalid deal id")


def validate_deal_id(deal_id: str) -> str:
    if not deal_id or "/" in deal_id or "\\" in deal_id or ".." in deal_id:
        raise _INVALID_DEAL
    return deal_id


def prose_path(deal_id: str) -> str:  # type: ignore[override]
    """Compatibility shim — returns a virtual path. Callers that write to disk
    should switch to save_prose."""
    return f"db://paper_prose/{validate_deal_id(deal_id)}"


def list_deal_ids(tenant_id: str) -> list[str]:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.list_paper_prose_deal_ids(%s)", (tid,))
        return [r[0] for r in cur.fetchall()]


def load_prose(deal_id: str, *, tenant_id: str) -> dict[str, dict[str, Any]] | None:
    deal_id = validate_deal_id(deal_id)
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.get_paper_prose(%s, %s)", (tid, deal_id))
        row = cur.fetchone()
    return row[0] if row else None


def save_prose(tenant_id: str, deal_id: str, prose: dict[str, Any]) -> None:
    tid = tenant_uuid(tenant_id)
    deal_id = validate_deal_id(deal_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT public.save_paper_prose(%s, %s, %s::jsonb)",
            (tid, deal_id, json.dumps(prose)),
        )
        if not cur.fetchone()[0]:
            raise ValueError("Prose does not belong to this tenant")
