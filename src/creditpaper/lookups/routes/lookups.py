"""Lookup API — GET /api/lookups.

Thin router: resolve auth, call lookup_fetch (migration rn_001_lookup_tables),
return the JSONB result. Platform-level reference data — no tenant scoping.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from creditpaper.auth.dependencies import CurrentUser, require_permission
from creditpaper.db import cursor

router = APIRouter(prefix="/api/lookups", tags=["lookups"])


@router.get("")
def fetch_lookups(
    type: str = "parent",
    key: str = "",
    _user: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    """List top-level lookups (type=parent) or direct children of key (type=child)."""
    with cursor() as cur:
        cur.execute("SELECT lookup_fetch(%s, %s)", (type, key))
        row = cur.fetchone()
    result = row[0] if row else []
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result
