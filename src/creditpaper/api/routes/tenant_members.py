"""Tenant members & invitations API — /api/tenant/*.

All data comes from stored procedures (migration 0037). Python is a thin
router: resolve auth, call the SP, return the JSONB result.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import CurrentUser, require_permission
from creditpaper.auth.supabase_invite import send_invite_email
from creditpaper.db import cursor, read_cursor, tenant_cursor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tenant", tags=["tenant"])

ALLOWED_ROLES = {"tenant_admin", "maker", "checker", "viewer"}


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class InviteRequest(BaseModel):
    email: str
    role: str


class MemberUpdateRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# GET /api/tenant/members — list all users in the tenant
# ---------------------------------------------------------------------------


@router.get("/members")
def list_members(
    user: CurrentUser = Depends(require_permission("users.read")),  # noqa: B008
):
    tenant_id = resolve_tenant_id(user)
    with cursor() as cur:
        cur.execute("SELECT get_tenant_members(%s)", (tenant_id,))
        row = cur.fetchone()
    return row[0] if row else []


# ---------------------------------------------------------------------------
# GET /api/tenant/invitations — list pending invitations
# ---------------------------------------------------------------------------


@router.get("/invitations")
def list_invitations(
    user: CurrentUser = Depends(require_permission("users.read")),  # noqa: B008
):
    tenant_id = resolve_tenant_id(user)
    with cursor() as cur:
        cur.execute("SELECT get_tenant_invitations(%s)", (tenant_id,))
        row = cur.fetchone()
    return row[0] if row else []


# ---------------------------------------------------------------------------
# POST /api/tenant/invite — create an invitation
# ---------------------------------------------------------------------------


@router.post("/invite")
def invite_user(
    req: InviteRequest,
    user: CurrentUser = Depends(require_permission("users.invite")),  # noqa: B008
):
    tenant_id = resolve_tenant_id(user)

    role = req.role.strip().lower()
    if role not in ALLOWED_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")

    email = req.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    token = secrets.token_urlsafe(32)

    with cursor() as cur:
        cur.execute(
            "SELECT create_tenant_invitation(%s, %s, %s, %s)",
            (tenant_id, email, role, token),
        )
        row = cur.fetchone()

    result = row[0] if row else {}
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    # Send invite email (best-effort — do not fail the request if email fails)
    try:
        tenant_name = None
        with read_cursor() as cur:
            cur.execute("SELECT name FROM tenants WHERE id::text = %s", (tenant_id,))
            r = cur.fetchone()
            if r:
                tenant_name = r[0]
        send_invite_email(email, role, token, tenant_name, inviter_email=user.email)
    except Exception:
        logger.exception("Failed to send invite email to %s", email)

    return result


# ---------------------------------------------------------------------------
# POST /api/tenant/resend-invite/{id} — reset expiry to +7 days
# ---------------------------------------------------------------------------


@router.post("/resend-invite/{invitation_id}")
def resend_invite(
    invitation_id: str,
    user: CurrentUser = Depends(require_permission("users.invite")),  # noqa: B008
):
    tenant_id = resolve_tenant_id(user)

    with cursor() as cur:
        cur.execute(
            "SELECT resend_tenant_invitation(%s, %s)",
            (invitation_id, tenant_id),
        )
        row = cur.fetchone()

    result = row[0] if row else {}
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])

    # Re-send invite email with same token (best-effort)
    try:
        token = None
        tenant_name = None
        with read_cursor() as cur:
            cur.execute("SELECT token FROM invitations WHERE id::text = %s", (invitation_id,))
            r = cur.fetchone()
            if r:
                token = r[0]
            cur.execute("SELECT name FROM tenants WHERE id::text = %s", (tenant_id,))
            r = cur.fetchone()
            if r:
                tenant_name = r[0]
        if token and isinstance(result, dict) and result.get("email"):
            send_invite_email(
                result["email"], result.get("role", "maker"), token, tenant_name, inviter_email=user.email
            )
    except Exception:
        logger.exception("Failed to re-send invite email for %s", invitation_id)

    return result


# ---------------------------------------------------------------------------
# DELETE /api/tenant/invite/{id} — cancel an invitation
# ---------------------------------------------------------------------------


@router.delete("/invite/{invitation_id}")
def cancel_invite(
    invitation_id: str,
    user: CurrentUser = Depends(require_permission("users.remove")),  # noqa: B008
):
    tenant_id = resolve_tenant_id(user)

    with cursor() as cur:
        cur.execute(
            "SELECT cancel_tenant_invitation(%s, %s)",
            (invitation_id, tenant_id),
        )
        row = cur.fetchone()

    return row[0] if row else {"deleted": False}


# ---------------------------------------------------------------------------
# PATCH /api/tenant/members/{id} — update role or is_active
# ---------------------------------------------------------------------------


@router.patch("/members/{member_id}")
def update_member(
    member_id: str,
    req: MemberUpdateRequest,
    user: CurrentUser = Depends(require_permission("users.update")),  # noqa: B008
):
    tenant_id = resolve_tenant_id(user)

    role = req.role.strip().lower() if req.role else None
    if role and role not in ALLOWED_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")

    # Self-deactivation and last-admin guards live inside the SP (rn_002);
    # it also writes the auth_audit_events row for role/activation changes.
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT update_tenant_member(%s, %s, %s, %s, %s)",
            (member_id, tenant_id, role, req.is_active, user.id),
        )
        row = cur.fetchone()

    result = row[0] if row else {}
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result
