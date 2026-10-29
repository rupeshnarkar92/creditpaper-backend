"""Deal Activity routes (contract 2026-09-15).

  GET /api/deals/{deal_id}/activity?category=documents|facts|paper|stage|deal&limit=50&cursor=...
      mounted under `/api/deals` through the workspace router
  GET /api/activity?category=...&limit=50&cursor=...
      the tenant's activity across its deals (Home); each event also carries `deal: {id, name}`

Read-only. Tenant-scoped: another tenant's deal answers 404 DEAL_NOT_FOUND. A bad limit,
category or cursor answers 422 in the error envelope.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.db import tenant_uuid
from creditpaper.deals.services import activity, home_dashboard
from creditpaper.deals.store import home_dashboard_store

router = APIRouter(tags=["deal-activity"])
# Home: GET /api/activity. Mounted in api/main.py (it is not under /api/deals).
tenant_activity_router = APIRouter(prefix="/api", tags=["deal-activity"])


def _err(request: Request, code: str, message: str, status: int, field: str | None = None) -> HTTPException:
    body: dict[str, object] = {
        "code": code,
        "message": message,
        "request_id": request.headers.get("x-request-id", ""),
    }
    if field:
        body["field"] = field
    return HTTPException(status_code=status, detail={"error": body})


@router.get("/{deal_id}/activity")
def get_activity(
    deal_id: str,
    request: Request,
    category: str | None = None,
    limit: str | None = None,
    cursor: str | None = None,
    tenant_id: str = Depends(resolve_tenant_id),
) -> dict[str, Any]:
    # `limit` is parsed here, not by FastAPI, so a bad value answers in the error envelope.
    page_size = _page_size(request, limit)
    try:
        tid = str(tenant_uuid(tenant_id))
    except ValueError:
        raise _err(request, "DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None
    try:
        return activity.list_activity(
            tid, deal_id, category=category or None, limit=page_size, cursor_token=cursor or None
        )
    except activity.InvalidActivityQuery as exc:
        raise _err(request, "INVALID_ACTIVITY_QUERY", exc.message, 422, exc.field) from None
    except activity.DealNotFound:
        raise _err(request, "DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None


def _page_size(request: Request, limit: str | None) -> int:
    try:
        return activity.DEFAULT_LIMIT if limit in (None, "") else int(str(limit))
    except ValueError:
        raise _err(
            request,
            "INVALID_ACTIVITY_QUERY",
            f"limit must be between 1 and {activity.MAX_LIMIT}.",
            422,
            "limit",
        ) from None


@tenant_activity_router.get("/home/dashboard")
def get_home_dashboard(
    tenant_id: str = Depends(resolve_tenant_id),
) -> dict[str, Any]:
    """Fetch the tenant's deals and five most recent activity events in one DB call."""
    try:
        tid = str(tenant_uuid(tenant_id))
    except ValueError:
        raise HTTPException(status_code=403, detail="Organisation membership required") from None
    raw = home_dashboard_store.get_home_dashboard(tid, activity_limit=5)
    return home_dashboard.present_home_dashboard(raw)


@tenant_activity_router.get("/activity")
def get_tenant_activity(
    request: Request,
    category: str | None = None,
    limit: str | None = None,
    cursor: str | None = None,
    tenant_id: str = Depends(resolve_tenant_id),
) -> dict[str, Any]:
    page_size = _page_size(request, limit)
    try:
        tid = str(tenant_uuid(tenant_id))
    except ValueError:
        raise _err(request, "TENANT_REQUIRED", "Organisation membership required", 403) from None
    try:
        return activity.list_tenant_activity(
            tid, category=category or None, limit=page_size, cursor_token=cursor or None
        )
    except activity.InvalidActivityQuery as exc:
        raise _err(request, "INVALID_ACTIVITY_QUERY", exc.message, 422, exc.field) from None
