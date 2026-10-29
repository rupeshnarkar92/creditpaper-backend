from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Response

from creditpaper.auth.config import get_settings

COOKIE_NAME = "creditpaper_session"
SESSION_ISSUER = "creditpaper-api"
SESSION_AUDIENCE = "creditpaper-app"


def _secret() -> str:
    secret = get_settings().JWT_SECRET_KEY
    if not secret or secret == "super-secret-key-change-in-production":
        raise RuntimeError("JWT_SECRET_KEY must be configured before issuing app sessions")
    return secret


def create_session_token(
    *,
    user_id: str,
    email: str,
    provider: str,
    mfa_ok: bool = False,
    password_session: bool = False,
) -> str:
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=get_settings().JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "provider": provider,
            # Raw facts, never the verdict: policy (tenants.require_mfa) is
            # re-evaluated per request so a policy flip lands within one
            # cookie lifetime. `pwd` separates password sign-ins (in scope
            # for TOTP per flow A) from OAuth ones; `mfa_ok` records that
            # MFA was satisfied at mint (aal2 or a consumed recovery code).
            "mfa_ok": mfa_ok,
            "pwd": password_session,
            "iss": SESSION_ISSUER,
            "aud": SESSION_AUDIENCE,
            "iat": now,
            "exp": expires,
        },
        _secret(),
        algorithm="HS256",
    )


def verify_session_token(token: str) -> dict | None:
    try:
        return jwt.decode(
            token,
            _secret(),
            algorithms=["HS256"],
            issuer=SESSION_ISSUER,
            audience=SESSION_AUDIENCE,
        )
    except (jwt.InvalidTokenError, RuntimeError):
        return None


def set_session_cookie(
    response: Response,
    *,
    user_id: str,
    email: str,
    provider: str,
    mfa_ok: bool = False,
    password_session: bool = False,
) -> None:
    response.set_cookie(
        COOKIE_NAME,
        create_session_token(
            user_id=user_id,
            email=email,
            provider=provider,
            mfa_ok=mfa_ok,
            password_session=password_session,
        ),
        httponly=True,
        secure=get_settings().COOKIE_SECURE,
        samesite="lax",
        max_age=get_settings().JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")
