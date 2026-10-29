"""Single-call, tenant-scoped read for the Home dashboard."""

from typing import Any

from creditpaper.db import tenant_cursor, tenant_uuid


def get_home_dashboard(tenant_id: str, *, activity_limit: int = 25) -> dict[str, Any]:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.get_home_dashboard(%s, %s)",
            (tenant_uuid(tenant_id), activity_limit),
        )
        return cur.fetchone()[0]
