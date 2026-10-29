"""Supabase JWT verification helper.

Supports both ES256 (default for newer Supabase projects) and HS256.
SUPABASE_JWT_SECRET can be:
  - A JWK set (JSON with "keys" array) for ES256
  - A PEM public key for ES256
  - A plain secret string for HS256 (legacy)
"""

import base64
import json
import os

import jwt
from cryptography.hazmat.primitives.asymmetric.ec import (
    SECP256R1,
    EllipticCurvePublicKey,
    EllipticCurvePublicNumbers,
)

SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")


def _b64url_decode(val: str) -> bytes:
    """Base64url decode with padding."""
    padding = 4 - len(val) % 4
    if padding != 4:
        val += "=" * padding
    return base64.urlsafe_b64decode(val)


def _jwk_to_ec_public_key(jwk: dict) -> EllipticCurvePublicKey:
    """Convert an EC JWK to a cryptography public key."""
    x = _b64url_decode(jwk["x"])
    y = _b64url_decode(jwk["y"])
    public_numbers = EllipticCurvePublicNumbers(
        x=int.from_bytes(x, "big"),
        y=int.from_bytes(y, "big"),
        curve=SECP256R1(),
    )
    return public_numbers.public_key()


def _load_es256_keys() -> list[EllipticCurvePublicKey]:
    """Parse SUPABASE_JWT_SECRET and return ES256 public keys."""
    secret = SUPABASE_JWT_SECRET.strip()
    if not secret:
        return []

    keys: list[EllipticCurvePublicKey] = []

    # PEM format
    if secret.startswith("-----BEGIN"):
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        try:
            key = load_pem_public_key(secret.encode())
            if isinstance(key, EllipticCurvePublicKey):
                keys.append(key)
        except Exception:
            pass
        return keys

    # JSON JWK or JWK Set
    if secret.startswith("{"):
        try:
            data = json.loads(secret)
            jwks = data.get("keys", [data]) if isinstance(data, dict) else []
            for jwk in jwks:
                if jwk.get("kty") == "EC" and jwk.get("alg") == "ES256":
                    keys.append(_jwk_to_ec_public_key(jwk))
        except Exception:
            pass
        return keys

    return []


_es256_keys: list[EllipticCurvePublicKey] = []


def _ensure_keys_loaded() -> None:
    """Lazily load ES256 keys on first use (env is ready by request time)."""
    global _es256_keys
    if _es256_keys is not None and len(_es256_keys) > 0:
        return
    # Re-read env in case it was set after module import
    secret = os.getenv("SUPABASE_JWT_SECRET", "").strip()
    if not secret:
        return
    keys: list[EllipticCurvePublicKey] = []
    if secret.startswith("{"):
        try:
            data = json.loads(secret)
            jwks = data.get("keys", [data]) if isinstance(data, dict) else []
            for jwk in jwks:
                if jwk.get("kty") == "EC" and jwk.get("alg") == "ES256":
                    keys.append(_jwk_to_ec_public_key(jwk))
        except Exception:
            pass
    elif secret.startswith("-----BEGIN"):
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        try:
            key = load_pem_public_key(secret.encode())
            if isinstance(key, EllipticCurvePublicKey):
                keys.append(key)
        except Exception:
            pass
    _es256_keys.clear()
    _es256_keys.extend(keys)


def verify_supabase_token(token: str) -> dict | None:
    """Verify a Supabase JWT and return the payload.

    Supports ES256 (newer Supabase) and HS256 (legacy).
    Returns None if the token is invalid, expired, or the JWT secret is not configured.

    A 60s leeway absorbs clock skew between the Supabase signer and this
    machine; without it a freshly issued token whose ``iat`` is seconds in
    the future is rejected with ``ImmatureSignatureError``.
    """
    jwt_secret = os.getenv("SUPABASE_JWT_SECRET", "")
    if not jwt_secret:
        return None

    _ensure_keys_loaded()

    # Try ES256 with each configured public key
    for pub_key in _es256_keys:
        try:
            return jwt.decode(
                token,
                pub_key,
                algorithms=["ES256"],
                audience="authenticated",
                leeway=60,
            )
        except jwt.InvalidTokenError:
            continue

    # Fallback: HS256 with the raw secret
    if not _es256_keys:
        try:
            return jwt.decode(
                token,
                jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
                leeway=60,
            )
        except jwt.InvalidTokenError:
            pass

    return None
