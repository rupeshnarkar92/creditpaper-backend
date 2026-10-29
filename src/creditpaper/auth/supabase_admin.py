"""Backend-only auth database access through restricted stored procedures."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from creditpaper.db import cursor, read_cursor

_PROCEDURES = {
    "provision_tenant_for_user": 3,
    "get_signup_email_status": 3,
    "resolve_application_user": 3,
    "get_supabase_auth_context": 1,
    "get_user_membership": 1,
    "get_authenticated_application_context": 6,
    "pending_invitation_for_email": 1,
    "record_auth_event": 8,
    "record_password_history": 4,
    "get_password_history_hashes": 2,
    "get_password_history_count": 1,
    "set_user_mfa_enrolled": 2,
    "get_tenant_mfa_policy": 1,
    "set_tenant_mfa_policy": 2,
}


class StoredProcedureUnavailable(RuntimeError):
    """A required authentication/signup database procedure could not run."""


def _run_read_procedure(function: str, placeholders: str, params: tuple) -> Any:
    """Execute an internal, fixed-name read-only stored procedure."""
    if function not in _PROCEDURES or _PROCEDURES[function] != len(params):
        raise ValueError("Unsupported stored procedure call")
    try:
        with read_cursor() as cur:
            cur.execute(f"SELECT public.{function}({placeholders})", params)
            row = cur.fetchone()
    except Exception as exc:
        raise StoredProcedureUnavailable(f"Required database function {function} failed") from exc
    return row[0] if row else None


def _run_write_procedure(function: str, placeholders: str, params: tuple) -> Any:
    """Execute an internal stored procedure inside a transaction."""
    if function not in _PROCEDURES or _PROCEDURES[function] != len(params):
        raise ValueError("Unsupported stored procedure call")
    try:
        with cursor() as cur:
            cur.execute(f"SELECT public.{function}({placeholders})", params)
            row = cur.fetchone()
    except Exception as exc:
        raise StoredProcedureUnavailable(f"Required database function {function} failed") from exc
    return row[0] if row else None


def provision_tenant(user_id: str, email: str, company_name: str) -> dict[str, Any] | None:
    """Create a tenant and initial admin membership atomically."""
    payload = _run_write_procedure("provision_tenant_for_user", "%s, %s, %s", (user_id, email, company_name))
    return payload if isinstance(payload, dict) else None


def signup_email_status(
    email: str = "", domain: str = "", exclude_user_id: str | None = None
) -> dict[str, Any] | None:
    """Return minimal duplicate/domain/provider status for signup and guards."""
    payload = _run_read_procedure("get_signup_email_status", "%s, %s, %s", (email, domain, exclude_user_id))
    return payload if isinstance(payload, dict) else None


def email_exists(email: str) -> bool | None:
    """Whether the exact email is registered, including unconfirmed users."""
    status = signup_email_status(email=email)
    return bool(status["email_registered"]) if status is not None else None


def domain_has_users(domain: str, exclude_user_id: str | None = None) -> bool | None:
    """Whether another Supabase user has this normalized email domain."""
    status = signup_email_status(domain=domain, exclude_user_id=exclude_user_id)
    return bool(status["auth_domain_registered"]) if status is not None else None


def pending_invitation_for_email(email: str) -> dict[str, Any] | None:
    """Return a pending, unexpired invitation for this address, if any."""
    payload = _run_read_procedure("pending_invitation_for_email", "%s", (email,))
    if isinstance(payload, dict) and payload.get("invited") is True:
        return payload
    return None


def resolve_application_user(user_id: str, email: str, provider: str) -> dict[str, Any] | None:
    """Create/update the local profile and resolve tenant RBAC in one DB call."""
    payload = _run_write_procedure("resolve_application_user", "%s, %s, %s", (user_id, email, provider))
    return payload if isinstance(payload, dict) else None


def set_user_mfa_enrolled(user_id: str, enrolled: bool) -> dict[str, Any] | None:
    """Set or clear the local MFA enrolment marker (AUTH-11).

    Only the marker — GoTrue factors remain the source of truth and are
    verified server-side before ``enrolled=True`` is passed here.
    """
    payload = _run_write_procedure("set_user_mfa_enrolled", "%s, %s", (user_id, enrolled))
    return payload if isinstance(payload, dict) else None


def tenant_mfa_policy(tenant_id: str) -> dict[str, Any] | None:
    """The firm's two-factor requirement and how many members have set it up."""
    payload = _run_read_procedure("get_tenant_mfa_policy", "%s", (tenant_id,))
    return payload if isinstance(payload, dict) else None


def set_tenant_mfa_policy(tenant_id: str, require_mfa: bool) -> dict[str, Any] | None:
    """Turn the firm's two-factor requirement on or off, returning the new state.

    The tenant comes from the signed-in session at the call site, never from the
    request body, so one firm cannot set another's policy.
    """
    payload = _run_write_procedure("set_tenant_mfa_policy", "%s, %s", (tenant_id, require_mfa))
    return payload if isinstance(payload, dict) else None


def authenticated_application_context(
    user_id: str,
    email: str,
    provider: str,
    provider_names: list[str],
    microsoft_tenant_id: str,
    include_onboarding: bool = False,
) -> dict[str, Any] | None:
    """Validate auth identity and resolve its application profile in one DB call."""
    payload = _run_write_procedure(
        "get_authenticated_application_context",
        "%s, %s, %s, %s, %s, %s",
        (user_id, email, provider, provider_names, microsoft_tenant_id, include_onboarding),
    )
    return payload if isinstance(payload, dict) else None


def ensure_application_user(user_id: str, email: str, provider: str) -> bool | None:
    """Compatibility wrapper returning only the linked profile active flag."""
    context = resolve_application_user(user_id, email, provider)
    return bool(context["is_active"]) if context is not None else None


def get_supabase_auth_context(user_id: str) -> dict[str, Any] | None:
    """Fetch active-state and non-secret provider identity claims in one call."""
    payload = _run_read_procedure("get_supabase_auth_context", "%s", (user_id,))
    return payload if isinstance(payload, dict) else None


def get_auth_user_state(user_id: str) -> dict[str, Any] | None:
    """Compatibility accessor for Supabase account state."""
    context = get_supabase_auth_context(user_id)
    if context is None:
        return None
    return {key: context.get(key) for key in ("banned_until", "deleted_at", "email_confirmed_at", "email")}


def get_sso_identity_context(user_id: str) -> list[dict[str, Any]]:
    """Compatibility accessor for provider identity metadata."""
    context = get_supabase_auth_context(user_id)
    identities = context.get("identities", []) if context else []
    return identities if isinstance(identities, list) else []


def get_auth_provider(email: str) -> dict[str, Any] | None:
    """Return coarse provider information for signup UX without token data."""
    status = signup_email_status(email=email)
    if status is None or not status.get("email_registered"):
        return None
    providers = status.get("providers")
    providers = providers if isinstance(providers, list) else []
    is_oauth_only = bool(status.get("is_oauth_only"))
    has_google = "google" in providers
    has_email = "email" in providers or not is_oauth_only
    return {
        "providers": providers,
        "is_oauth_only": is_oauth_only,
        "primary": status.get("primary", "unknown"),
        "has_google": has_google,
        "has_email": has_email,
    }


def user_membership(user_id: str) -> dict[str, Any] | None:
    """Resolve tenant and RBAC authorization in one stored-procedure call."""
    payload = _run_read_procedure("get_user_membership", "%s", (user_id,))
    return payload if isinstance(payload, dict) else None


def record_auth_event_procedure(
    *,
    event_id: str,
    tenant_id: str | None,
    user_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str | None,
    event_metadata: dict[str, Any],
    correlation_id: str,
) -> dict[str, Any] | None:
    """Insert a structured audit event via the dedicated database function."""
    payload = _run_write_procedure(
        "record_auth_event",
        "%s, %s, %s, %s, %s, %s, %s, %s",
        (event_id, tenant_id, user_id, action, entity_type, entity_id, Jsonb(event_metadata), correlation_id),
    )
    return payload if isinstance(payload, dict) else None


def record_password_history_procedure(
    *, event_id: str, user_id: str, password_hash: str, keep_count: int
) -> int:
    """Persist a pre-hashed password and atomically trim the retained history."""
    value = _run_write_procedure(
        "record_password_history", "%s, %s, %s, %s", (event_id, user_id, password_hash, keep_count)
    )
    return int(value or 0)


def password_history_hashes(user_id: str, limit: int) -> list[str]:
    """Fetch a bounded list of salted hashes for local Argon2 checks."""
    value = _run_read_procedure("get_password_history_hashes", "%s, %s", (user_id, limit))
    return value if isinstance(value, list) else []


def password_history_count(user_id: str) -> int:
    """Return password history count through its stored routine."""
    value = _run_read_procedure("get_password_history_count", "%s", (user_id,))
    return int(value or 0)
