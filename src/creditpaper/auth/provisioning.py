"""First-run tenant provisioning for Supabase-authenticated users.

The stored procedure creates the tenant and initial admin membership in one
transaction. Retries for an existing member remain idempotent.
"""

from __future__ import annotations

from creditpaper.auth.supabase_admin import StoredProcedureUnavailable, provision_tenant
from creditpaper.auth.tenant_domain import DomainAlreadyRegistered, require_company_domain
from creditpaper.db import read_cursor, tenant_cursor

TENANT_ADMIN_ROLE = "tenant_admin"


def provision_tenant_for_user(user_id: str, email: str | None, company_name: str | None) -> dict:
    """Provision an active tenant and the user's initial admin membership."""
    domain = require_company_domain(email or "")
    try:
        result = provision_tenant(user_id, email or "", company_name or "")
    except StoredProcedureUnavailable as exc:
        raise RuntimeError("Tenant provisioning is temporarily unavailable") from exc
    if result is None or not isinstance(result, dict):
        raise RuntimeError("Tenant provisioning did not return a valid result")
    if result.get("status") == "DOMAIN_TAKEN":
        raise DomainAlreadyRegistered(domain)
    if result.get("status") == "USER_NOT_AUTHORIZED":
        raise PermissionError("A verified active Supabase user is required")
    if result.get("status") != "OK" or not result.get("tenant_id"):
        raise RuntimeError("Tenant provisioning failed")
    return {"tenant_id": str(result["tenant_id"]), "created": bool(result.get("created"))}


def accept_pending_invitation(user_id: str, email: str | None) -> dict | None:
    """Join the inviting firm when this address holds a usable invitation.

    Returns {"tenant_id", "role"} when an unaccepted, unexpired invitation
    matches the email, or None otherwise. The invitation flow itself (token
    check, password) lives in api/routes/invite.py; this mirrors its
    membership SQL for the one case it cannot reach — a user who
    self-signed-up or returned from SSO instead of finishing the token flow.
    Without this, complete-signup sees the inviting firm's domain as taken
    and rejects the person the firm explicitly invited.
    """
    if not email:
        return None
    with read_cursor() as cur:
        cur.execute(
            """
            SELECT id, domain_id, role
            FROM invitations
            WHERE lower(email) = lower(%s)
              AND accepted = false
              AND (expires_at IS NULL OR expires_at > now())
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (email,),
        )
        invitation = cur.fetchone()
    if invitation is None:
        return None
    invitation_id, tenant_id, role_name = (
        str(invitation[0]),
        str(invitation[1]),
        str(invitation[2]),
    )
    with tenant_cursor(tenant_id) as cur:
        # Tenant-specific first, then global (seeded with tenant_id IS NULL)
        # — same lookup api/routes/invite.py uses for the token flow.
        cur.execute(
            "SELECT id FROM roles WHERE (tenant_id::text = %s OR tenant_id IS NULL) "
            "AND name = %s ORDER BY tenant_id NULLS LAST LIMIT 1",
            (tenant_id, role_name),
        )
        role_row = cur.fetchone()
    if role_row is None:
        raise RuntimeError(f"Invitation role is not seeded: {role_name}")
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "INSERT INTO user_roles (user_id, role_id, tenant_id, assigned_by) "
            "SELECT %s, %s, %s, %s WHERE NOT EXISTS ("
            "  SELECT 1 FROM user_roles WHERE user_id = %s AND tenant_id::text = %s)",
            (user_id, str(role_row[0]), tenant_id, user_id, user_id, tenant_id),
        )
        # The auto-created local row carries the provisional admin defaults;
        # make it match the invitation, as set-password does for new users.
        cur.execute(
            "UPDATE users SET role = %s, domain_id = %s WHERE id::text = %s",
            (role_name, tenant_id, user_id),
        )
        cur.execute(
            "UPDATE invitations SET accepted = true WHERE id::text = %s AND accepted = false",
            (invitation_id,),
        )
    return {"tenant_id": tenant_id, "role": role_name}
