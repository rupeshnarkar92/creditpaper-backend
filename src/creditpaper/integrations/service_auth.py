"""Signed service-to-service auth for independently deployed workloads."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import HTTPException, Request

ISSUER = "creditpaper-service-auth"
MAX_TOKEN_SECONDS = 300


def _secret() -> str:
    secret = os.getenv("SERVICE_AUTH_SECRET", "").strip()
    if len(secret) < 32:
        raise RuntimeError("SERVICE_AUTH_SECRET must be configured with at least 32 characters")
    return secret


def issue_service_token(*, service: str, tenant_id: str, audience: str, ttl_seconds: int = 120) -> str:
    if not 1 <= ttl_seconds <= MAX_TOKEN_SECONDS:
        raise ValueError("Service token lifetime must be between 1 and 300 seconds")
    if not service or len(service) > 100 or not audience or len(audience) > 160:
        raise ValueError("Service and audience identifiers are required and length-limited")
    try:
        tenant = str(uuid.UUID(tenant_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Service token requires a valid organization UUID") from None
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": service,
            "tenant_id": tenant,
            "aud": audience,
            "iss": ISSUER,
            "iat": now,
            "exp": now + timedelta(seconds=ttl_seconds),
            "jti": os.urandom(16).hex(),
        },
        _secret(),
        algorithm="HS256",
    )


def verify_service_request(request: Request, *, audience: str) -> dict[str, Any]:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Service authentication required")
    try:
        claims = jwt.decode(
            token,
            _secret(),
            algorithms=["HS256"],
            issuer=ISSUER,
            audience=audience,
            options={"require": ["sub", "tenant_id", "iat", "exp", "jti"]},
        )
        if not isinstance(claims.get("sub"), str) or not isinstance(claims.get("tenant_id"), str):
            raise jwt.InvalidTokenError("invalid claims")
        if len(claims["sub"]) > 100:
            raise jwt.InvalidTokenError("invalid service")
        uuid.UUID(claims["tenant_id"])
        issued_at = int(claims["iat"])
        expires_at = int(claims["exp"])
        if expires_at <= issued_at or expires_at - issued_at > MAX_TOKEN_SECONDS:
            raise jwt.InvalidTokenError("token lifetime too long")
        if issued_at > int(datetime.now(UTC).timestamp()) + 30:
            raise jwt.InvalidTokenError("token issued in the future")
        return claims
    except (jwt.InvalidTokenError, ValueError, TypeError, RuntimeError):
        raise HTTPException(status_code=401, detail="Invalid service credentials") from None
