"""GoTrue factor queries (AUTH-11) via the caller's own credentials.

The browser could claim enrolment without completing it; the marker that
drives ``MFA_REQUIRED`` vs ``MFA_ENROLL_REQUIRED`` must be confirmed against
GoTrue itself. ``GET /auth/v1/user`` with the request's Supabase bearer plus
the publishable apikey returns the caller's factors, so no service-role
secret is required anywhere. Fail-closed: missing configuration, a failed
call, or an identity mismatch yields no factors, so ``mfa/enrolled`` refuses
to set the marker.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def list_totp_factors(user_id: str, access_token: str) -> list[dict]:
    """The user's MFA factors via ``GET /auth/v1/user`` (self lookup).

    Returns the raw factor list (``status`` is ``verified``/``unverified``);
    ``[]`` when Supabase isn't configured, the call fails, or the returned
    user id does not match ``user_id``.
    """
    import httpx

    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_ANON_KEY", "")
    if not url or not key or not access_token:
        logger.warning("SUPABASE_URL/SUPABASE_ANON_KEY not configured - no factors visible")
        return []
    try:
        resp = httpx.get(
            f"{url}/auth/v1/user",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {access_token}",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("GoTrue user lookup failed: %s %s", resp.status_code, resp.text[:300])
            return []
        data = resp.json()
        if not isinstance(data, dict) or data.get("id") != user_id:
            logger.warning("GoTrue user lookup identity mismatch for %s", user_id)
            return []
        factors = data.get("factors")
        return factors if isinstance(factors, list) else []
    except Exception:
        logger.exception("GoTrue user lookup error for %s", user_id)
        return []


def has_verified_totp_factor(user_id: str, access_token: str) -> bool:
    """True when the user has at least one verified TOTP factor."""
    return any(
        str(factor.get("factor_type", factor.get("type", ""))).lower() == "totp"
        and factor.get("status") == "verified"
        for factor in list_totp_factors(user_id, access_token)
    )
