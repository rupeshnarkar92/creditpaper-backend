"""Supabase auth routes — signup, login, onboarding, dashboard."""

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from creditpaper.auth.app_session import clear_session_cookie, set_session_cookie
from creditpaper.auth.auth_audit import (
    ACTION_INVITATION_ACCEPTED,
    ACTION_LOGIN_SUCCESS,
    ACTION_LOGOUT,
    ACTION_MFA_ENROLLED,
    ACTION_MFA_POLICY_CHANGED,
    ACTION_MFA_UNENROLLED,
    ACTION_SIGNUP_COMPLETED,
    ACTION_SIGNUP_INITIATED,
    record_auth_event,
)
from creditpaper.auth.dependencies import CurrentUser, get_current_user, require_permission, security
from creditpaper.auth.domain_validator import extract_domain
from creditpaper.auth.password_history import password_was_used, record_password
from creditpaper.auth.provisioning import accept_pending_invitation, provision_tenant_for_user
from creditpaper.auth.supabase_admin import (
    StoredProcedureUnavailable,
    pending_invitation_for_email,
    set_tenant_mfa_policy,
    set_user_mfa_enrolled,
    signup_email_status,
    tenant_mfa_policy,
)
from creditpaper.auth.supabase_mfa import has_verified_totp_factor
from creditpaper.auth.tenant_domain import (
    DomainAlreadyRegistered,
    DomainNotCompany,
    guard_domain_for_signup,
    require_company_domain,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# One sentence everywhere a personal domain is refused — backend routes and
# frontend screens all surface this exact copy.
MSG_PERSONAL_DOMAIN = (
    "Personal email domains (Gmail, Yahoo, etc.) are not allowed. "
    "Please sign in with your company Google account."
)
# One sentence everywhere a taken firm domain is refused. The domain is the
# tenant key: someone from the firm already signed up, so the person needs an
# invite, not another account.
MSG_DOMAIN_TAKEN = "This domain already exists. Contact admin."


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


# SEC-09: every field on an anonymous request body is bounded. These routes
# accept unauthenticated traffic and write straight to the audit log or drive a
# live database lookup, so an unbounded string is a free log-poisoning and
# storage-amplification vector and an over-long address is a cheap way to burn
# a call on parsing. Supabase's own address ceiling is 320 characters
# (RFC 5321), so 320/254 is generous without being open-ended.
_MAX_EMAIL = 320
_MAX_DOMAIN = 255
_MAX_LABEL = 64
_MAX_ERROR = 256


class ValidateDomainRequest(BaseModel):
    email: str = Field(..., max_length=_MAX_EMAIL)  # noqa: A003


class CheckEmailRequest(BaseModel):
    email: str = Field(..., max_length=_MAX_EMAIL)  # noqa: A003


class CompleteSignupRequest(BaseModel):
    name: str = Field(..., max_length=120)
    company_name: str = Field(..., max_length=200)


class AuditEventRequest(BaseModel):
    """Client-reported auth telemetry.

    `error` is free text written straight into the audit log, so it is capped
    and stripped of control characters before it is stored.
    """

    email: str | None = Field(default=None, max_length=_MAX_EMAIL)
    auth_source: str = Field(default="email", max_length=_MAX_LABEL)
    domain: str | None = Field(default=None, max_length=_MAX_DOMAIN)
    error: str | None = Field(default=None, max_length=_MAX_ERROR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_record_event(**kwargs: Any) -> None:
    """Record an audit event, swallowing DB errors so the request never fails."""
    try:
        record_auth_event(**kwargs)
    except Exception:
        logger.exception("Failed to record auth audit event (action=%s)", kwargs.get("action"))


def _has_pending_invitation(email: str) -> bool:
    """True when a usable invitation lets this address through signup checks.

    Fails closed: any lookup problem keeps the pre-existing 409 behaviour.
    """
    try:
        return pending_invitation_for_email(email) is not None
    except Exception:
        logger.exception("Pending invitation lookup failed for signup check")
        return False


def _clean(value: str | None) -> str | None:
    """Strip control characters from client-supplied audit text.

    SEC-09: `error` and `domain` arrive unauthenticated and land in a durable
    log. Newlines and NULs let one caller forge the shape of a line, and
    bidi-override characters can make a stored entry read differently than it
    is. Collapse whitespace rather than rejecting — the caller is a browser
    reporting a Supabase error string, not an attacker with a schema.
    """
    if value is None:
        return None
    stripped = "".join(ch if (ch.isprintable() or ch == " ") else " " for ch in value)
    return " ".join(stripped.split()) or None


# ---------------------------------------------------------------------------
# Domain / email validation
# ---------------------------------------------------------------------------


@router.post("/validate-domain")
async def validate_domain(request: ValidateDomainRequest):
    """Validate that email uses a company domain (not personal email).

    Also reports whether the domain is free for a new firm, so the signup
    screen can show "contact your admin" before any account is created.
    """
    try:
        domain = guard_domain_for_signup(request.email)
    except DomainNotCompany:
        raise HTTPException(status_code=400, detail=MSG_PERSONAL_DOMAIN) from None
    except ValueError:
        raise HTTPException(status_code=400, detail="A valid work email is required") from None
    except DomainAlreadyRegistered as exc:
        # A pending invitation is the intended door into an existing firm:
        # let signup proceed; provisioning joins the inviting tenant later.
        if _has_pending_invitation(request.email):
            return {"valid": True, "domain": exc.domain, "domain_available": True}
        raise HTTPException(status_code=409, detail=MSG_DOMAIN_TAKEN) from None
    except (RuntimeError, StoredProcedureUnavailable):
        raise HTTPException(status_code=503, detail="Signup validation is temporarily unavailable") from None
    return {"valid": True, "domain": domain, "domain_available": True}


@router.post("/check-email")
async def check_email(request: CheckEmailRequest):
    """Check what exists for an email — UX feedback only, no account data.

    Deliberately coarse: it leaks at most "a firm exists on this domain" and,
    when the service-role key is configured, "this exact address is already
    registered" — never roles, ids or account state. The signup screen uses
    it for live on-blur validation; the login screen uses the domain flag.
    """
    base = {
        "available": True,
        "domain_registered": False,
        "personal_domain": False,
        # Unknown (None) is reported as False plus the flag below, so callers
        # can distinguish "we know it is free" from "we could not check".
        "email_registered": False,
        "email_check_available": True,
        "auth_provider": None,
        "is_oauth_only": False,
    }
    try:
        domain = require_company_domain(request.email)
    except DomainNotCompany:
        return {**base, "personal_domain": True}
    except ValueError:
        return {**base, "available": False, "email_check_available": True}
    try:
        status = signup_email_status(email=request.email, domain=domain)
    except StoredProcedureUnavailable:
        return {**base, "email_check_available": False}
    if status is None:
        return {**base, "email_check_available": False}
    domain_registered = bool(status.get("domain_registered"))
    if domain_registered and _has_pending_invitation(request.email):
        # The signup screen treats this flag as a hard stop; an invited
        # address reads as available and is joined by provisioning instead.
        domain_registered = False
    return {
        **base,
        "available": not domain_registered,
        "domain_registered": domain_registered,
        "email_registered": bool(status.get("email_registered")),
        "email_check_available": True,
        "auth_provider": status.get("primary"),
        "is_oauth_only": bool(status.get("is_oauth_only")),
    }


# ---------------------------------------------------------------------------
# Signup completion
# ---------------------------------------------------------------------------


@router.post("/complete-signup")
async def complete_signup(
    request: CompleteSignupRequest,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
):
    """Complete signup after Supabase email verification.

    Creates the firm (tenant) and the user's admin membership, then records
    the audit event. Idempotent — retrying never creates a second firm.
    """
    company = (request.company_name or "").strip()
    if not company:
        raise HTTPException(status_code=400, detail="Company name is required.")
    # Authoritative backstop: the frontend pre-checks these, but a crafted
    # request (or a Google account on a personal domain) lands here.
    try:
        require_company_domain(user.email or "")
    except DomainNotCompany:
        _safe_record_event(
            action="signup.blocked_personal_domain",
            entity_type="user",
            user_id=user.id,
            event_metadata={"email": user.email, "auth_source": "email"},
        )
        raise HTTPException(status_code=403, detail=MSG_PERSONAL_DOMAIN) from None
    except ValueError:
        raise HTTPException(status_code=400, detail="A valid work email is required") from None
    # An invited email that self-signed-up (or returned from SSO) joins the
    # inviting firm — the invitation is the permission to be on that domain.
    # Without this branch the firm's own domain reads as taken (409) and the
    # invited person is rejected.
    try:
        joined = accept_pending_invitation(user.id, user.email)
    except Exception:
        logger.exception("Invitation join failed for user_id=%s", user.id)
        raise HTTPException(
            status_code=503, detail="Signup provisioning is temporarily unavailable"
        ) from None
    if joined is not None:
        tenant_id = joined["tenant_id"]
        _safe_record_event(
            action=ACTION_INVITATION_ACCEPTED,
            entity_type="tenant",
            entity_id=tenant_id,
            tenant_id=tenant_id,
            user_id=user.id,
            event_metadata={
                "email": user.email,
                "domain": extract_domain(user.email or ""),
                "role": joined["role"],
                "auth_source": "email",
            },
        )
        return {"message": "Signup completed", "tenant_id": tenant_id}
    try:
        membership = provision_tenant_for_user(user.id, user.email, company)
    except DomainAlreadyRegistered:
        raise HTTPException(status_code=409, detail=MSG_DOMAIN_TAKEN) from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="A verified active account is required") from None
    except (RuntimeError, StoredProcedureUnavailable):
        raise HTTPException(
            status_code=503, detail="Signup provisioning is temporarily unavailable"
        ) from None
    except Exception:
        logger.exception("Signup provisioning failed for user_id=%s", user.id)
        raise HTTPException(
            status_code=503, detail="Signup provisioning is temporarily unavailable"
        ) from None
    tenant_id = membership["tenant_id"]
    _safe_record_event(
        action=ACTION_SIGNUP_COMPLETED,
        entity_type="tenant",
        entity_id=tenant_id,
        tenant_id=tenant_id,
        user_id=user.id,
        event_metadata={
            "email": user.email,
            "domain": extract_domain(user.email or ""),
            "company_name": company,
            "auth_source": "email",
        },
    )
    return {"message": "Signup completed", "tenant_id": tenant_id}


# ---------------------------------------------------------------------------
# SSO guard — validations for the Google return path
# ---------------------------------------------------------------------------


@router.post("/sso-guard")
async def sso_guard(
    response: Response,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
):
    """Validate a Google-authenticated user's email before anything is provisioned.

    The OAuth round trip cannot pre-check the address, so the callback page
    calls this with the session token before routing. Pass or fail — never
    creates or modifies anything.
    """
    email = user.email or ""
    # Existing members must be allowed to log in even though their domain is
    # already claimed by a tenant. Only reject public/personal domains here;
    # the domain-taken check belongs to first-time tenant provisioning.
    try:
        domain = require_company_domain(email)
    except (DomainNotCompany, ValueError):
        _safe_record_event(
            action="signup.blocked_personal_domain",
            entity_type="user",
            user_id=user.id,
            event_metadata={"email": email, "auth_source": "google"},
        )
        raise HTTPException(status_code=403, detail=MSG_PERSONAL_DOMAIN) from None
    if not user.email:
        raise HTTPException(status_code=401, detail="Authenticated email required")
    set_session_cookie(
        response,
        user_id=user.id,
        email=user.email,
        provider=user.auth_provider,
        mfa_ok=user.mfa_ok,
        password_session=user.password_session,
    )
    _safe_record_event(
        action=ACTION_LOGIN_SUCCESS,
        entity_type="user",
        user_id=user.id,
        tenant_id=user.tenant_id,
        event_metadata={"email": email, "auth_source": "sso"},
    )
    return {"allowed": True, "domain": domain, **_profile_response(user)}


# ---------------------------------------------------------------------------
# Auth audit — frontend calls these to record events after Supabase actions
# ---------------------------------------------------------------------------


def _profile_response(user: CurrentUser) -> dict[str, Any]:
    """Build the frontend's current-user payload from the authorized user."""
    meta = user.metadata
    first_name = meta.get("first_name")
    last_name = meta.get("last_name")
    name = f"{first_name or ''} {last_name or ''}".strip() or None
    onboarding_completed = user.onboarding_completed
    return {
        "id": user.id,
        "email": user.email,
        "auth_provider": user.auth_provider,
        "created_at": user.created_at,
        "role": user.role,
        "is_super_admin": user.is_super_admin,
        "domain_id": user.domain_id,
        "tenant_id": user.tenant_id,
        "name": name,
        "first_name": first_name,
        "last_name": last_name,
        "job_title": meta.get("job_title"),
        "phone": meta.get("phone"),
        "onboarding_completed": onboarding_completed,
        "permissions": user.permissions,
    }


@router.post("/session")
async def create_application_session(
    response: Response,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
):
    """Issue the app cookie and return the profile from this authorization pass."""
    if not user.email:
        raise HTTPException(status_code=401, detail="Authenticated email required")
    set_session_cookie(
        response,
        user_id=user.id,
        email=user.email,
        provider=user.auth_provider,
        mfa_ok=user.mfa_ok,
        password_session=user.password_session,
    )
    return {"authenticated": True, **_profile_response(user)}


# ---------------------------------------------------------------------------
# MFA (AUTH-11) — enrolment marker; challenge/recovery live client-side
# ---------------------------------------------------------------------------


class MfaEnrolledRequest(BaseModel):
    enrolled: bool = True


@router.post("/mfa/enrolled")
async def mfa_enrolled(
    body: MfaEnrolledRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),  # noqa: B008
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
):
    """Set or clear the local enrolment marker after GoTrue verification.

    Called by the browser right after ``mfa.verify`` succeeds (enrol) or
    ``mfa.unenroll`` succeeds (turn off). The caller's Supabase bearer is
    re-checked against GoTrue (``GET /auth/v1/user`` — fail-closed, no
    service-role key needed): a client claiming enrolment without a verified
    factor gets 400 and no marker. The marker feeds enforcement
    (MFA_REQUIRED vs MFA_ENROLL_REQUIRED) and AccountTab status; it is never
    the source of truth for whether a factor exists.
    """
    if body.enrolled and not (credentials and has_verified_totp_factor(user.id, credentials.credentials)):
        raise HTTPException(status_code=400, detail="No verified authenticator app is enrolled")
    result = set_user_mfa_enrolled(user.id, body.enrolled)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise HTTPException(status_code=503, detail="MFA enrolment state could not be saved")
    _safe_record_event(
        action=ACTION_MFA_ENROLLED if body.enrolled else ACTION_MFA_UNENROLLED,
        entity_type="user",
        user_id=user.id,
        tenant_id=user.tenant_id,
        event_metadata={"email": user.email, "auth_source": "supabase"},
    )
    return {
        "mfa_enrolled": result.get("mfa_enrolled") is True,
        "mfa_enrolled_at": result.get("mfa_enrolled_at"),
    }


# ---------------------------------------------------------------------------
# MFA policy (AUTH-11) — the firm's decision to require two-factor
# ---------------------------------------------------------------------------


class MfaPolicyRequest(BaseModel):
    require_mfa: bool


def _policy_response(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "require_mfa": payload.get("require_mfa") is True,
        "members": int(payload.get("members") or 0),
        "enrolled": int(payload.get("enrolled") or 0),
    }


@router.get("/mfa/policy")
async def get_mfa_policy(
    user: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    """The firm's two-factor requirement and how many members have set it up.

    Readable by any member holding ``settings.read`` so the Team screen can show
    the same switch state the sign-in enforcement is using.
    """
    if not user.tenant_id:
        raise HTTPException(status_code=404, detail="No firm membership")
    try:
        payload = tenant_mfa_policy(user.tenant_id)
    except StoredProcedureUnavailable:
        raise HTTPException(status_code=503, detail="Two-factor policy is temporarily unavailable") from None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise HTTPException(status_code=404, detail="No firm membership")
    return _policy_response(payload)


@router.put("/mfa/policy")
async def put_mfa_policy(
    body: MfaPolicyRequest,
    user: CurrentUser = Depends(require_permission("settings.update")),  # noqa: B008
):
    """Require (or stop requiring) two-factor for this firm's password sign-ins.

    Admin only (``settings.update``). The firm is the caller's own — taken from the
    session, never the request — so no body field can reach another tenant. The
    change is audited with its before and after value.

    Turning the policy on does not lock anyone out mid-session: it takes effect at
    each member's next password sign-in, and members with no factor are sent to
    enrol first (MFA_ENROLL_REQUIRED).
    """
    if not user.tenant_id:
        raise HTTPException(status_code=404, detail="No firm membership")
    try:
        payload = set_tenant_mfa_policy(user.tenant_id, body.require_mfa)
    except StoredProcedureUnavailable:
        raise HTTPException(status_code=503, detail="Two-factor policy could not be saved") from None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise HTTPException(status_code=404, detail="No firm membership")
    _safe_record_event(
        action=ACTION_MFA_POLICY_CHANGED,
        entity_type="tenant",
        entity_id=user.tenant_id,
        user_id=user.id,
        tenant_id=user.tenant_id,
        event_metadata={
            "previous": payload.get("previous") is True,
            "require_mfa": payload.get("require_mfa") is True,
            "enrolled": int(payload.get("enrolled") or 0),
            "members": int(payload.get("members") or 0),
        },
    )
    return _policy_response(payload)


@router.post("/audit/signup-initiated")
async def audit_signup_initiated(
    request: AuditEventRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
):
    """Record signup initiated event."""
    background_tasks.add_task(
        _safe_record_event,
        action=ACTION_SIGNUP_INITIATED,
        entity_type="user",
        user_id=user.id,
        tenant_id=user.tenant_id,
        event_metadata={
            "email": _clean(request.email),
            "domain": _clean(request.domain) or extract_domain(request.email or ""),
            "auth_source": _clean(request.auth_source) or "email",
        },
    )
    return {"recorded": True}


@router.post("/audit/login-success")
async def audit_login_success(
    request: AuditEventRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
):
    """Record successful login event."""
    background_tasks.add_task(
        _safe_record_event,
        action="login.success",
        entity_type="user",
        user_id=user.id,
        tenant_id=user.tenant_id,
        event_metadata={
            "email": _clean(request.email) or user.email,
            "auth_source": _clean(request.auth_source) or "email",
        },
    )
    return {"recorded": True}


@router.post("/audit/login-failed")
async def audit_login_failed(request: AuditEventRequest):
    """Record failed login attempt (no auth required).

    SEC-09: unauthenticated by necessity — there is no session to present when
    a login fails — so the route is bounded instead of trusted. Fields are
    length-capped on the model and scrubbed of control characters here, and the
    per-IP budget in `api/rate_limit.py` stops it being used to flood the audit
    table. A production deployment should move this behind a Supabase auth hook
    so failed logins are recorded server-side against a verified identity; that
    needs dashboard configuration and is tracked as SEC-09 residual.
    """
    _safe_record_event(
        action="login.failed",
        entity_type="user",
        event_metadata={
            "email": _clean(request.email),
            "auth_source": _clean(request.auth_source) or "email",
            "error": _clean(request.error),
        },
    )
    return {"recorded": True}


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


@router.post("/logout")
async def logout(response: Response, user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    """Record logout event. Supabase signOut is called on the frontend."""
    _safe_record_event(
        action=ACTION_LOGOUT,
        entity_type="user",
        user_id=user.id,
        tenant_id=user.tenant_id,
        event_metadata={"email": user.email},
    )
    clear_session_cookie(response)
    return {"message": "Logged out"}


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@router.get("/me")
async def get_me(user: CurrentUser = Depends(get_current_user)):  # noqa: B008
    """Get current user profile with roles and permissions."""
    return _profile_response(user)


# ---------------------------------------------------------------------------
# Password history — the last 5 passwords can never be reused
# ---------------------------------------------------------------------------


class PasswordHistoryCheckRequest(BaseModel):
    user_id: str = Field(..., max_length=64)
    password: str = Field(..., min_length=1, max_length=1024)


class PasswordHistoryRecordRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=1024)


@router.post("/password-history/check")
async def check_password_history(
    request: PasswordHistoryCheckRequest,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
):
    """Is this candidate one of the signed-in user's last 5 passwords?

    Authenticated, and the user id comes from the verified token rather than
    the request body. An open version of this route is a password oracle: an
    attacker holding a target's user id could test candidate passwords
    remotely, unthrottled, with no lockout and no session. Both callers
    (the Supabase recovery reset and Settings -> Account) already hold a live
    `supabase.auth.getUser()` session, so requiring one costs them nothing.

    The body's `user_id` is still accepted and checked, so a stale client
    cannot probe another account by sending a different id.
    """
    if not request.password:
        raise HTTPException(status_code=400, detail="Password is required.")
    if request.user_id != str(user.id):
        raise HTTPException(status_code=403, detail="Not permitted.")
    used = password_was_used(str(user.id), request.password)
    return {"was_used": used}


@router.post("/password-history")
async def add_password_history(
    request: PasswordHistoryRecordRequest,
    user: CurrentUser = Depends(get_current_user),  # noqa: B008
):
    """Record the password just set (reset screen and Settings→Account).

    Authenticated: only the signed-in user can grow their own history, and
    the row attaches to the token's user id — the request carries no id.
    """
    if not request.password:
        raise HTTPException(status_code=400, detail="Password is required.")
    record_password(user.id, request.password)
    return {"recorded": True}
