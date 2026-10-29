"""Tenant-bound persistence for integration and MCP configuration."""

from __future__ import annotations

import json
import uuid
from typing import Any

from creditpaper.auth.security.encryption import decrypt, decrypt_envelope, encrypt_envelope
from creditpaper.db import tenant_cursor, tenant_uuid
from creditpaper.integrations.security import safe_details, validate_public_https_url


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid resource identifier") from None


def _encrypt_credentials(data: dict[str, Any], tenant_id: str, record_id: str, purpose: str) -> str:
    return encrypt_envelope(data, tenant_id=tenant_id, record_id=record_id, purpose=purpose)


def _decrypt_credentials(token: str, tenant_id: str, record_id: str, purpose: str) -> dict:
    if token.startswith(("v2:", "v3:")):
        return decrypt_envelope(token, tenant_id=tenant_id, record_id=record_id, purpose=purpose)
    # Legacy direct-KEK records remain readable during the bounded migration.
    return decrypt(token)


def _audit(
    cur,
    tenant_id: str,
    actor_id: str | None,
    resource_type: str,
    resource_id: str | None,
    action: str,
    details: dict | None = None,
) -> None:
    actor_user = None
    actor_service = None
    if actor_id:
        try:
            actor_user = str(uuid.UUID(actor_id))
        except (ValueError, TypeError, AttributeError):
            actor_service = actor_id[:100]
    cur.execute(
        "SELECT public.record_integration_audit(%s,%s,%s,%s,%s,%s,%s::jsonb)",
        (
            tenant_id,
            actor_user,
            actor_service,
            resource_type,
            resource_id,
            action,
            json.dumps(safe_details(details)),
        ),
    )


def list_integrations(tenant_id: str) -> list[dict[str, Any]]:
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.list_tenant_integrations(%s)", (tenant_uuid(tenant_id),))
        rows = cur.fetchall()
    return [
        {
            "id": str(row[0]),
            "name": row[1],
            "provider": row[2],
            "base_url": row[3],
            "auth_type": row[4],
            "config": row[5],
            "enabled": row[6],
            "created_at": row[7].isoformat() if row[7] else None,
            "updated_at": row[8].isoformat() if row[8] else None,
            "configured": True,
        }
        for row in rows
    ]


def upsert_integration(
    tenant_id: str,
    actor_id: str,
    *,
    resource_id: str | None,
    name: str,
    provider: str,
    base_url: str,
    auth_type: str,
    credentials: dict[str, Any] | None,
    config: dict[str, Any],
    rate_limit_per_minute: int,
) -> dict[str, Any]:
    endpoint = validate_public_https_url(base_url, label="Integration base URL")
    if auth_type not in {"api_key", "bearer", "oauth_client_credentials", "webhook"}:
        raise ValueError("Unsupported integration authentication type")
    credential_fields = {
        "api_key": {"api_key", "token", "access_token"},
        "bearer": {"token", "access_token"},
        "oauth_client_credentials": {"client_id", "client_secret"},
        "webhook": {"webhook_secret"},
    }[auth_type]
    required_credentials = {
        "api_key": ("api_key", "token", "access_token"),
        "bearer": ("token", "access_token"),
        "oauth_client_credentials": ("client_id", "client_secret"),
        "webhook": ("webhook_secret",),
    }[auth_type]
    if credentials is not None and (
        set(credentials) - credential_fields
        or not any(credentials.get(field) for field in required_credentials)
    ):
        raise ValueError("Integration credentials do not match the selected authentication type")
    if credentials is None and not resource_id:
        raise ValueError("Credentials are required when creating an integration")
    rid = _uuid(resource_id) if resource_id else str(uuid.uuid4())
    with tenant_cursor(tenant_id) as cur:
        existing = None
        if resource_id:
            cur.execute(
                "SELECT * FROM public.integration_credentials(%s,%s)",
                (tenant_id, rid),
            )
            existing = cur.fetchone()
            if not existing:
                raise ValueError("Integration not found")
            if credentials is None and (existing[1] != auth_type or existing[2] != provider):
                raise ValueError(
                    "Credentials are required when changing integration provider or authentication"
                )
        if credentials is not None:
            encrypted = _encrypt_credentials(credentials, tenant_id, rid, "integration_credentials")
        elif existing:
            encrypted = str(existing[0])
        else:
            raise ValueError("Credentials are required")

        if resource_id:
            cur.execute(
                "SELECT public.update_tenant_integration(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
                (
                    tenant_id,
                    rid,
                    name,
                    provider,
                    endpoint,
                    auth_type,
                    encrypted,
                    json.dumps(safe_details(config)),
                    rate_limit_per_minute,
                ),
            )
            result = cur.fetchone()
            if not result or result[0] is None:
                raise ValueError("Integration not found")
            action = "updated"
        else:
            cur.execute(
                "SELECT public.create_tenant_integration(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
                (
                    rid,
                    tenant_id,
                    name,
                    provider,
                    endpoint,
                    auth_type,
                    encrypted,
                    json.dumps(safe_details(config)),
                    rate_limit_per_minute,
                    actor_id,
                ),
            )
            result = cur.fetchone()
            action = "created"
        new_id = str(result[0])
        _audit(
            cur,
            tenant_id,
            actor_id,
            "integration",
            new_id,
            f"integration.{action}",
            {"name": name, "provider": provider, "auth_type": auth_type},
        )
    return {
        "id": new_id,
        "name": name,
        "provider": provider,
        "base_url": endpoint,
        "auth_type": auth_type,
        "configured": True,
    }


def delete_integration(tenant_id: str, actor_id: str, resource_id: str) -> None:
    rid = _uuid(resource_id)
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.delete_tenant_integration(%s,%s)",
            (tenant_id, rid),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("Integration not found")
        _audit(cur, tenant_id, actor_id, "integration", rid, "integration.deleted", {"name": row[0]})


def integration_runtime_config(tenant_id: str, resource_id: str) -> dict[str, Any] | None:
    rid = _uuid(resource_id)
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.integration_runtime_row(%s,%s)", (tenant_uuid(tenant_id), rid))
        row = cur.fetchone()
    if not row:
        return None
    credentials = _decrypt_credentials(str(row[5]), tenant_id, rid, "integration_credentials")
    if not isinstance(credentials, dict):
        raise RuntimeError("Integration credentials are invalid")
    return {
        "id": str(row[0]),
        "name": row[1],
        "provider": row[2],
        "base_url": row[3],
        "auth_type": row[4],
        "credentials": credentials,
        "config": row[6] or {},
        "rate_limit_per_minute": int(row[7]),
    }


def authorize_integration_call(tenant_id: str, resource_id: str) -> dict[str, Any] | None:
    """Atomically enforce and consume the integration's per-minute request quota."""
    rid = _uuid(resource_id)
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.integration_runtime_row(%s,%s)", (tenant_uuid(tenant_id), rid))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            "SELECT public.consume_integration_quota(%s,%s,%s)",
            (tenant_id, rid, row[7]),
        )
        allowed = cur.fetchone()
        if not allowed:
            _audit(
                cur,
                tenant_id,
                None,
                "integration",
                rid,
                "integration.rate_limited",
                {"name": row[1], "limit_per_minute": row[7]},
            )
            return {"rate_limited": True}
    credentials = _decrypt_credentials(str(row[5]), tenant_id, rid, "integration_credentials")
    return {
        "id": str(row[0]),
        "name": row[1],
        "provider": row[2],
        "base_url": row[3],
        "auth_type": row[4],
        "credentials": credentials,
        "config": row[6] or {},
        "rate_limit_per_minute": int(row[7]),
        "rate_limited": False,
    }


def webhook_secret(tenant_id: str, resource_id: str) -> str | None:
    """Read a webhook verification secret from one tenant-owned integration."""
    rid = _uuid(resource_id)
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT public.webhook_integration_secret_row(%s,%s)", (tenant_uuid(tenant_id), rid))
        row = cur.fetchone()
    if not row:
        return None
    credentials = _decrypt_credentials(str(row[0]), tenant_id, rid, "integration_credentials")
    secret = credentials.get("webhook_secret") if isinstance(credentials, dict) else None
    return secret if isinstance(secret, str) and secret else None


def record_webhook_event(
    tenant_id: str,
    integration_id: str,
    event_id: str,
    *,
    payload_digest: str,
) -> bool:
    """Atomically enqueue acceptance of a previously authenticated unique webhook event."""
    rid = _uuid(integration_id)
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.accept_integration_webhook(%s,%s,%s,%s)",
            (tenant_id, rid, event_id, payload_digest),
        )
        result = cur.fetchone()
        status = result[0] if result else -1
        if status != 1:
            if status == -1:
                return False
            _audit(
                cur,
                tenant_id,
                "webhook",
                "integration",
                rid,
                "integration.webhook.replay_rejected",
                {"event_id": event_id},
            )
            return False
        _audit(
            cur,
            tenant_id,
            "webhook",
            "integration",
            rid,
            "integration.webhook.received",
            {"event_id": event_id},
        )
        return True


def record_integration_call(
    tenant_id: str,
    actor_id: str,
    resource_id: str,
    *,
    status: str,
    attempts: int,
    action: str = "integration.request",
) -> None:
    rid = _uuid(resource_id)
    with tenant_cursor(tenant_id) as cur:
        _audit(cur, tenant_id, actor_id, "integration", rid, action, {"status": status, "attempts": attempts})


def list_mcp_servers(tenant_id: str) -> list[dict[str, Any]]:
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.list_tenant_mcp_servers(%s)", (tenant_uuid(tenant_id),))
        rows = cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "name": r[1],
            "endpoint": r[2],
            "auth_type": r[3],
            "allowed_tools": r[4] or [],
            "allowed_resources": r[5] or [],
            "approved": r[6],
            "enabled": r[7],
            "created_at": r[8].isoformat() if r[8] else None,
            "updated_at": r[9].isoformat() if r[9] else None,
            "configured": True,
            "runtime_enabled": False,
        }
        for r in rows
    ]


def upsert_mcp_server(
    tenant_id: str,
    actor_id: str,
    *,
    resource_id: str | None,
    name: str,
    endpoint: str,
    auth_type: str,
    credentials: dict[str, Any] | None,
    allowed_tools: list[str],
    allowed_resources: list[str],
) -> dict[str, Any]:
    safe_endpoint = validate_public_https_url(endpoint, label="MCP server endpoint")
    if auth_type not in {"none", "bearer", "api_key"}:
        raise ValueError("Unsupported MCP authentication type")
    if auth_type == "none" and credentials is not None:
        raise ValueError("Credentials are not allowed with auth_type=none")
    credential_fields = {"bearer": {"token", "access_token"}, "api_key": {"api_key"}}
    if (
        auth_type != "none"
        and credentials is not None
        and (
            not credentials
            or set(credentials) - credential_fields[auth_type]
            or not any(credentials.get(field) for field in credential_fields[auth_type])
        )
    ):
        raise ValueError("MCP credentials do not match the selected authentication type")
    rid = _uuid(resource_id) if resource_id else str(uuid.uuid4())
    with tenant_cursor(tenant_id) as cur:
        old_credentials = None
        was_approved = False
        if resource_id:
            cur.execute(
                "SELECT * FROM public.mcp_server_credentials_for_update(%s,%s)",
                (tenant_id, rid),
            )
            old = cur.fetchone()
            if not old:
                raise ValueError("MCP server not found")
            old_credentials, was_approved = old[0], old[1]
            if credentials is None and auth_type != "none" and old[2] != auth_type:
                raise ValueError("Credentials are required when changing MCP authentication")
        if credentials is not None:
            encrypted = _encrypt_credentials(credentials, tenant_id, rid, "mcp_server_credentials")
        elif auth_type == "none":
            encrypted = None
        elif old_credentials:
            encrypted = str(old_credentials)
        else:
            raise ValueError("Credentials are required when creating this MCP server")
        if resource_id:
            cur.execute(
                "SELECT public.update_tenant_mcp_server(%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)",
                (
                    tenant_id,
                    rid,
                    name,
                    safe_endpoint,
                    auth_type,
                    encrypted,
                    json.dumps(sorted(set(allowed_tools))),
                    json.dumps(sorted(set(allowed_resources))),
                ),
            )
            new_id = str(cur.fetchone()[0])
            action = "updated"
        else:
            cur.execute(
                "SELECT public.create_tenant_mcp_server(%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)",
                (
                    rid,
                    tenant_id,
                    name,
                    safe_endpoint,
                    auth_type,
                    encrypted,
                    json.dumps(sorted(set(allowed_tools))),
                    json.dumps(sorted(set(allowed_resources))),
                    actor_id,
                ),
            )
            new_id = str(cur.fetchone()[0])
            action = "created"
        _audit(
            cur,
            tenant_id,
            actor_id,
            "mcp_server",
            new_id,
            f"mcp_server.{action}",
            {"name": name, "approved_before_update": was_approved},
        )
    return {
        "id": new_id,
        "name": name,
        "endpoint": safe_endpoint,
        "auth_type": auth_type,
        "approved": False,
        "runtime_enabled": False,
    }


def set_mcp_approval(tenant_id: str, actor_id: str, resource_id: str, *, approved: bool) -> None:
    rid = _uuid(resource_id)
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.set_tenant_mcp_approval(%s,%s,%s)",
            (tenant_id, rid, approved),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            raise ValueError("MCP server not found")
        _audit(
            cur,
            tenant_id,
            actor_id,
            "mcp_server",
            rid,
            "mcp_server.approved" if approved else "mcp_server.revoked",
            {"name": row[0]},
        )


def delete_mcp_server(tenant_id: str, actor_id: str, resource_id: str) -> None:
    rid = _uuid(resource_id)
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.delete_tenant_mcp_server(%s,%s)",
            (tenant_id, rid),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            raise ValueError("MCP server not found")
        _audit(cur, tenant_id, actor_id, "mcp_server", rid, "mcp_server.deleted", {"name": row[0]})
