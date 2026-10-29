"""Auth audit logging helpers.

Records structured audit events through a restricted PostgreSQL function. Audit
metadata is limited to safe identity/context; tokens and credentials are never
accepted by this API.
"""

import uuid
from typing import Any

from creditpaper.auth.supabase_admin import record_auth_event_procedure

# Action slugs for signup flow
ACTION_SIGNUP_INITIATED = "signup.initiated"
ACTION_SIGNUP_EMAIL_VERIFIED = "signup.email_verified"
ACTION_SIGNUP_COMPLETED = "signup.completed"
ACTION_SIGNUP_FAILED = "signup.failed"
ACTION_LOGIN_INITIATED = "login.initiated"
ACTION_LOGIN_SUCCESS = "login.success"
ACTION_LOGIN_FAILED = "login.failed"
ACTION_LOGOUT = "logout"
ACTION_PASSWORD_RESET_REQUESTED = "password_reset.requested"
ACTION_PASSWORD_RESET_COMPLETED = "password_reset.completed"
ACTION_INVITATION_SENT = "invitation.sent"
ACTION_INVITATION_ACCEPTED = "invitation.accepted"
ACTION_ROLE_ASSIGNED = "role.assigned"
ACTION_ROLE_REMOVED = "role.removed"
# Two-factor (AUTH-11): a member's own enrolment, and the firm's policy decision.
ACTION_MFA_ENROLLED = "mfa.enrolled"
ACTION_MFA_UNENROLLED = "mfa.unenrolled"
ACTION_MFA_POLICY_CHANGED = "mfa.policy_changed"


def record_auth_event(
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    event_metadata: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Record a structured auth event through its database procedure."""
    try:
        result = record_auth_event_procedure(
            event_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            event_metadata=event_metadata or {},
            correlation_id=correlation_id or str(uuid.uuid4()),
        )
    except Exception as exc:
        raise RuntimeError("Authentication audit database routine failed") from exc
    if result is None:
        raise RuntimeError("Auth audit procedure did not return an event")
    return result
