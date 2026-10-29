"""RBAC helpers — permission checking for Supabase auth."""

from typing import Any

from creditpaper.auth.supabase_admin import StoredProcedureUnavailable, user_membership


class MembershipUnavailable(RuntimeError):
    """The required membership database function could not be called."""


def _membership_payload(user_id: str) -> dict[str, Any]:
    try:
        payload = user_membership(user_id)
    except StoredProcedureUnavailable as exc:
        raise MembershipUnavailable("Could not resolve the user's membership") from exc
    return payload if isinstance(payload, dict) else {}


def get_user_membership(user_id: str) -> tuple[str | None, list[str], list[str]]:
    """Resolve the active tenant, its roles, and permissions in one DB call."""
    payload = _membership_payload(user_id)
    return (
        payload.get("tenant_id"),
        payload.get("roles") or [],
        payload.get("permissions") or [],
    )


def get_user_permissions(user_id: str, tenant_id: str | None = None) -> list[str]:
    """Get permissions for the user's active tenant, optionally scoped."""
    if not tenant_id:
        return []
    resolved_tenant_id, _, permissions = get_user_membership(user_id)
    return permissions if resolved_tenant_id == tenant_id else []


def get_user_roles(user_id: str, tenant_id: str | None = None) -> list[str]:
    """Get role names for the user's active tenant, optionally scoped."""
    if not tenant_id:
        return []
    resolved_tenant_id, roles, _ = get_user_membership(user_id)
    return roles if resolved_tenant_id == tenant_id else []


def has_permission(user_id: str, tenant_id: str | None, permission: str) -> bool:
    """Check whether a user has a permission in the resolved active tenant."""
    if not tenant_id:
        return False
    resolved_tenant_id, _, permissions = get_user_membership(user_id)
    return resolved_tenant_id == tenant_id and permission in permissions
