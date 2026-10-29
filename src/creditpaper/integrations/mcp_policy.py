"""MCP authorization boundary; deliberately no remote tool execution adapter yet."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from creditpaper.db import tenant_cursor
from creditpaper.integrations.store import _audit, _uuid


def authorize_mcp_operation(
    tenant_id: str, actor_service: str, server_id: str, *, operation: str, resource: str
) -> dict[str, Any]:
    """Check approval and exact server-owned allowlists before any MCP action."""
    rid = _uuid(server_id)
    if operation not in {"tool", "resource"}:
        raise HTTPException(status_code=403, detail="MCP operation is not permitted")
    denied = False
    row = None
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """SELECT name, endpoint, auth_type, encrypted_credentials,
                      allowed_tools, allowed_resources, approved, enabled
                 FROM tenant_mcp_servers WHERE tenant_id=%s AND id=%s""",
            (tenant_id, rid),
        )
        row = cur.fetchone()
        permitted_list = []
        if row:
            permitted_list = row[4] or [] if operation == "tool" else row[5] or []
        denied = not row or not row[6] or not row[7] or resource not in permitted_list
        _audit(
            cur,
            tenant_id,
            actor_service,
            "mcp_server",
            rid,
            "mcp_server.access_denied" if denied else "mcp_server.access_authorized",
            {"operation": operation, "resource": resource, "service": actor_service},
        )
    if denied:
        raise HTTPException(status_code=403, detail="MCP operation is not approved")
    assert row is not None
    return {
        "server_id": rid,
        "name": row[0],
        "endpoint": row[1],
        "auth_type": row[2],
        "encrypted_credentials": str(row[3]) if row[3] else None,
        "operation": operation,
        "resource": resource,
    }
