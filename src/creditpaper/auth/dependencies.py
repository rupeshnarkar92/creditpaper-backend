"""FastAPI dependencies for Supabase auth + RBAC."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from creditpaper.auth.app_session import verify_session_token
from creditpaper.auth.auth_audit import record_auth_event
from creditpaper.auth.config import get_settings
from creditpaper.auth.domain_validator import is_valid_company_domain
from creditpaper.auth.rbac import MembershipUnavailable, has_permission
from creditpaper.auth.supabase import verify_supabase_token
from creditpaper.auth.supabase_admin import (
    StoredProcedureUnavailable,
    authenticated_application_context,
)

MSG_BUSINESS_EMAIL_REQUIRED = (
    "Personal email domains (Gmail, Yahoo, etc.) are not allowed. "
    "Please sign in with your company email address."
)
MSG_SSO_POLICY_REQUIRED = "This account is not authorized for the configured company SSO."
MSG_ACCOUNT_INACTIVE = "This account is inactive. Contact your administrator."
# Exact codes the frontend matches on (toApiError passes string detail through
# as message): MFA_REQUIRED -> challenge step, MFA_ENROLL_REQUIRED -> forced
# enrolment. Codes, not prose — the UI owns the friendly copy.
MSG_MFA_REQUIRED = "MFA_REQUIRED"
MSG_MFA_ENROLL_REQUIRED = "MFA_ENROLL_REQUIRED"
MSG_EMAIL_UNCONFIRMED = "Confirm your email address before signing in."

security = HTTPBearer(auto_error=False)


def _audit_rejection(action: str, user_id: str | None, email: str | None) -> None:
    try:
        record_auth_event(
            action=action,
            entity_type="user",
            user_id=user_id,
            event_metadata={"email": email, "auth_source": "supabase"},
        )
    except Exception:
        # Authentication must fail closed even if the audit database is unavailable.
        pass


def _provider_names(payload: dict) -> set[str]:
    app_meta = payload.get("app_metadata") or {}
    names: set[str] = set()
    provider = app_meta.get("provider") or payload.get("provider")
    if isinstance(provider, str):
        names.add(provider.lower())
    providers = app_meta.get("providers")
    if isinstance(providers, list):
        names.update(str(value).lower() for value in providers)
    return names


def _application_provider(payload: dict) -> str:
    providers = _provider_names(payload)
    for provider in ("google", "azure", "microsoft"):
        if provider in providers:
            return provider
    return "email"


def _mfa_verdict(payload: dict, *, via_cookie: bool) -> tuple[bool, bool]:
    """(password_session, mfa_ok) for this credential.

    Supabase path: GoTrue JWT claims — ``amr`` says how the session authenticated,
    ``aal`` says whether a second factor was presented. ``amr`` entries may be
    plain strings or ``{method, timestamp}`` objects. App-cookie path: the two
    facts written at mint, because the app cookie carries the verdict rather than
    the GoTrue token it came from — without them a cookie session would be asked
    for a second factor again on every call. Both an authenticator code and a
    consumed recovery code present a second factor, so both arrive as ``aal2``.
    """
    if via_cookie:
        return payload.get("pwd") is True, payload.get("mfa_ok") is True
    methods: set[str] = set()
    for item in payload.get("amr") or []:
        if isinstance(item, dict):
            item = item.get("method")
        if isinstance(item, str):
            methods.add(item)
    return "password" in methods, payload.get("aal") == "aal2"


def _authorize_active_account(state: dict | None, *, user_id: str | None = None) -> None:
    """Server-side backstop for account standing. Never rely on the client.

    SEC-08: Supabase's own `email_confirmed_at` is the authority on whether the
    address is proven. It was fetched into the auth context and then ignored, so
    the only thing refusing an unconfirmed account was `is_email_verified` set
    by the browser — trivially bypassed by calling the API directly. Only
    tenant provisioning checked the flag, which left every pre-provisioning
    route (workspace reads, `/api/auth/me`, `/api/auth/session`) reachable with
    an unverified token.

    A provider-issued session with no confirmed address is refused here. When
    Supabase has email confirmation switched off it stamps `email_confirmed_at`
    at signup, so this does not lock out a project configured that way.
    """
    if state is None:
        raise HTTPException(status_code=401, detail="Authenticated user could not be resolved")
    now = datetime.now(UTC)
    banned_until = state.get("banned_until")
    if isinstance(banned_until, str):
        try:
            banned_until = datetime.fromisoformat(banned_until.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=503, detail="Authenticated user state is invalid") from None
    if banned_until is not None and banned_until.tzinfo is None:
        banned_until = banned_until.replace(tzinfo=UTC)
    if state.get("deleted_at") is not None or (banned_until is not None and banned_until > now):
        raise HTTPException(status_code=403, detail=MSG_ACCOUNT_INACTIVE)
    if state.get("email_confirmed_at") is None:
        _audit_rejection("login.blocked_unconfirmed_email", user_id, _state_email(state))
        raise HTTPException(status_code=403, detail=MSG_EMAIL_UNCONFIRMED)


def _state_email(state: dict) -> str | None:
    """The address on the Supabase row, when the context carried one."""
    email = state.get("email")
    return email if isinstance(email, str) else None


@dataclass
class CurrentUser:
    """The signed-in person, resolved from a Supabase JWT + local RBAC rows."""

    id: str
    email: str | None = None
    auth_provider: str = "email"
    created_at: str | None = None
    tenant_id: str | None = None
    roles: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    onboarding_completed: bool = False
    is_super_admin: bool = False
    metadata: dict = field(default_factory=dict)
    # MFA verdict facts (AUTH-11) — minted into the app cookie by /session.
    mfa_ok: bool = False
    password_session: bool = False
    # Legacy model surface kept for call-site compatibility.
    domain_id: str | None = None

    @property
    def role(self) -> str:
        """Primary role, or prospective firm admin before signup completion."""
        return self.roles[0] if self.roles else "tenant_admin"


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),  # noqa: B008
) -> CurrentUser:
    """Verify identity and resolve current profile, organization, and RBAC."""
    payload = None
    via_cookie = False
    if credentials is not None:
        payload = verify_supabase_token(credentials.credentials)
    if payload is None:
        session_token = request.cookies.get("creditpaper_session")
        if session_token:
            payload = verify_session_token(session_token)
            via_cookie = payload is not None
    if not payload:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_id = payload.get("sub")
    email = payload.get("email")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    if not isinstance(email, str) or not is_valid_company_domain(email):
        _audit_rejection("login.blocked_public_domain", user_id, email if isinstance(email, str) else None)
        raise HTTPException(status_code=403, detail=MSG_BUSINESS_EMAIL_REQUIRED)

    provider_names = sorted(_provider_names(payload))
    settings = get_settings()
    application_provider = _application_provider(payload)
    include_onboarding = request.url.path in {
        "/api/auth/me",
        "/api/auth/session",
        "/api/auth/sso-guard",
    }
    try:
        # Sync psycopg on the pooler: ~0.4-2.4s per call. Left on the event loop
        # it serialises every other request behind it (superadmin list pages
        # appeared to hang at 25s+ under three concurrent calls). Off-loop, one
        # request's round trip no longer freezes the server; the pool is
        # psycopg_pool.ConnectionPool, which is thread-safe.
        context = await asyncio.to_thread(
            authenticated_application_context,
            user_id,
            email,
            application_provider,
            provider_names,
            settings.SUPABASE_MICROSOFT_TENANT_ID,
            include_onboarding,
        )
    except StoredProcedureUnavailable:
        raise HTTPException(status_code=503, detail="Authenticated user could not be resolved") from None
    if context is None:
        raise HTTPException(status_code=401, detail="Authenticated user could not be resolved")

    auth_context = context.get("auth_context")
    if not isinstance(auth_context, dict):
        raise HTTPException(status_code=401, detail="Authenticated user could not be resolved")
    _authorize_active_account(auth_context, user_id=user_id)

    if context.get("sso_allowed") is not True:
        _audit_rejection("login.blocked_sso_policy", user_id, email)
        raise HTTPException(status_code=403, detail=MSG_SSO_POLICY_REQUIRED)

    application_context = context.get("application_context")
    if not isinstance(application_context, dict):
        raise HTTPException(status_code=503, detail="Application profile could not be resolved")
    if not application_context.get("is_active"):
        raise HTTPException(status_code=403, detail=MSG_ACCOUNT_INACTIVE)

    # Tenant MFA policy (AUTH-11, flow A): TOTP applies to password sign-ins
    # only — OAuth sessions inherit no factor requirement. Dormant until an
    # admin sets tenants.require_mfa. Covers /session, /me and every API call
    # because they all resolve through here.
    password_session, mfa_ok = _mfa_verdict(payload, via_cookie=via_cookie)
    if application_context.get("require_mfa") is True and password_session and not mfa_ok:
        if application_context.get("mfa_enrolled_at"):
            raise HTTPException(status_code=403, detail=MSG_MFA_REQUIRED)
        raise HTTPException(status_code=403, detail=MSG_MFA_ENROLL_REQUIRED)

    metadata = payload.get("user_metadata") or {}
    return CurrentUser(
        id=user_id,
        email=email,
        auth_provider=application_provider,
        created_at=auth_context.get("created_at"),
        tenant_id=application_context.get("tenant_id"),
        roles=application_context.get("roles") or [],
        permissions=application_context.get("permissions") or [],
        onboarding_completed=application_context.get("onboarding_completed") is True,
        is_super_admin=application_context.get("is_super_admin") is True,
        metadata=metadata if isinstance(metadata, dict) else {},
        mfa_ok=mfa_ok,
        password_session=password_session,
    )


def ensure_permission(user: CurrentUser, permission: str) -> None:
    """Raise 403 unless the user holds the permission; for checks that depend on the request body."""
    try:
        allowed = has_permission(user.id, user.tenant_id, permission)
    except MembershipUnavailable:
        raise HTTPException(status_code=503, detail="Authorization is temporarily unavailable") from None
    if not allowed:
        raise HTTPException(status_code=403, detail="Insufficient permissions")


def require_permission(permission: str):
    """Dependency that checks if the current user has a specific permission."""

    async def dependency(user: CurrentUser = Depends(get_current_user)):  # noqa: B008
        ensure_permission(user, permission)
        return user

    # Readable by tests and audits: which permission a route demands.
    dependency.creditpaper_permission = permission  # type: ignore[attr-defined]
    return dependency


def require_any_permission(*permissions: str):
    """Dependency that allows the request when the user holds any one of the permissions."""

    async def dependency(user: CurrentUser = Depends(get_current_user)):  # noqa: B008
        try:
            allowed = any(has_permission(user.id, user.tenant_id, permission) for permission in permissions)
        except MembershipUnavailable:
            raise HTTPException(status_code=503, detail="Authorization is temporarily unavailable") from None
        if not allowed:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    dependency.creditpaper_permissions = tuple(permissions)  # type: ignore[attr-defined]
    return dependency


def require_role(*roles: str):
    """Dependency that checks if a user has one of the specified roles.

    Legacy gate. The API enforces permissions (auth/rbac.py); role gates remain only
    where a role is the thing being asserted, and every guarded write names a
    permission so a grant change takes effect without touching route code.
    """

    async def dependency(user: CurrentUser = Depends(get_current_user)):  # noqa: B008
        if not any(role in user.roles for role in roles):
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user

    dependency.creditpaper_roles = tuple(roles)  # type: ignore[attr-defined]
    return dependency


WRITER_ROLES = ("maker", "checker", "tenant_admin")
APPROVER_ROLES = ("checker", "tenant_admin")

ADMIN_ONLY = require_role("tenant_admin")
WRITER = require_role(*WRITER_ROLES)
APPROVER = require_role(*APPROVER_ROLES)
