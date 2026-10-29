"""Security policy shared by outbound integration adapters."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import socket
import time
from urllib.parse import urlsplit

from fastapi import HTTPException


def validate_public_https_url(value: str, *, label: str = "Endpoint") -> str:
    """Require a credential-free HTTPS URL whose DNS answers are all public."""
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.port is not None and not 1 <= parsed.port <= 65535)
    ):
        raise ValueError(f"{label} must be a credential-free HTTPS URL without query or fragment")
    try:
        addresses = {
            ipaddress.ip_address(answer[4][0])
            for answer in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError):
        raise ValueError(f"{label} host could not be resolved") from None
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError(f"{label} host must resolve only to public IP addresses")
    return candidate.rstrip("/")


def verify_webhook_signature(
    secret: str,
    tenant_id: str,
    integration_id: str,
    timestamp: str,
    event_id: str,
    body: bytes,
    signature: str,
    *,
    now: int | None = None,
) -> bool:
    """Verify an integration-bound HMAC with a five-minute replay window."""
    if not secret or not event_id or len(event_id) > 200:
        return False
    try:
        signed_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((int(time.time()) if now is None else now) - signed_at) > 300:
        return False
    message = f"{tenant_id}.{integration_id}.{timestamp}.{event_id}.".encode() + body
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def validate_integration_config(value: dict, *, auth_type: str) -> dict:
    """Allow only the non-secret settings consumed by supported auth adapters."""
    if not isinstance(value, dict) or len(value) > 8:
        raise ValueError("Integration config must be an object with at most eight fields")
    allowed = {
        "api_key": {"api_key_header"},
        "bearer": set(),
        "oauth_client_credentials": {"token_url", "scope"},
        "webhook": set(),
    }.get(auth_type)
    if allowed is None or set(value) - allowed:
        raise ValueError("Integration config contains unsupported fields")
    cleaned = dict(value)
    if "api_key_header" in cleaned and (
        not isinstance(cleaned["api_key_header"], str)
        or cleaned["api_key_header"].lower() not in {"x-api-key", "authorization"}
    ):
        raise ValueError("API key header must be X-API-Key or Authorization")
    if "token_url" in cleaned:
        if not isinstance(cleaned["token_url"], str):
            raise ValueError("OAuth token URL must be text")
        cleaned["token_url"] = validate_public_https_url(cleaned["token_url"], label="OAuth token URL")
    if "scope" in cleaned and (
        not isinstance(cleaned["scope"], str)
        or len(cleaned["scope"]) > 1000
        or any(ord(char) < 32 for char in cleaned["scope"])
    ):
        raise ValueError("OAuth scope must be plain text shorter than 1000 characters")
    return cleaned


def safe_details(value: dict | None) -> dict:
    """Recursively strip credential fields before audit/config JSON is persisted."""
    forbidden = {"key", "api_key", "token", "secret", "password", "authorization", "client_secret"}

    def clean(item):
        if isinstance(item, dict):
            return {
                str(key): clean(child)
                for key, child in item.items()
                if str(key).lower() not in forbidden
                and "secret" not in str(key).lower()
                and not str(key).lower().endswith("_token")
            }
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item

    return clean(value or {})


def secret_fields(value: dict, *, label: str = "Credentials") -> dict[str, str]:
    """Accept only a small fixed set of credential fields (no arbitrary headers)."""
    if not isinstance(value, dict) or not value or len(value) > 8:
        raise ValueError(f"{label} must contain between one and eight credential fields")
    allowed = {
        "api_key",
        "token",
        "access_token",
        "refresh_token",
        "webhook_secret",
        "client_id",
        "client_secret",
        "scope",
    }
    if any(key not in allowed for key in value):
        raise ValueError(f"{label} contain an unsupported field")
    if any(
        not isinstance(item, str) or len(item) > 4096 or any(ord(char) < 32 for char in item)
        for item in value.values()
    ):
        raise ValueError(f"{label} values must be plain text shorter than 4096 characters")
    return {key: item.strip() for key, item in value.items() if item.strip()}


def sanitize_remote_error(status_code: int) -> HTTPException:
    """Translate remote HTTP failures without forwarding response bodies or headers."""
    if status_code == 429:
        return HTTPException(status_code=429, detail="Integration provider rate limit reached")
    if status_code >= 500:
        return HTTPException(status_code=502, detail="Integration provider is temporarily unavailable")
    return HTTPException(status_code=502, detail="Integration provider rejected the request")
