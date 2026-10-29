import base64
import json
import os
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_key_cache = None
_envelope_key_cache: dict[str, bytes] = {}


def get_key() -> bytes:
    global _key_cache
    if _key_cache:
        return _key_cache

    key_str = os.getenv("ENCRYPTION_KEY", "")
    if not key_str:
        from creditpaper.auth.config import get_settings

        settings = get_settings()
        key_str = settings.ENCRYPTION_KEY

    if not key_str:
        raise ValueError("ENCRYPTION_KEY not set in environment or .env")

    _key_cache = base64.b64decode(key_str)
    return _key_cache


def encrypt(data: dict) -> str:
    key = get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    plaintext = json.dumps(data).encode()
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ciphertext).decode()


def decrypt(token: str) -> dict:
    key = get_key()
    aesgcm = AESGCM(key)
    raw = base64.b64decode(token)
    nonce = raw[:12]
    ciphertext = raw[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext)


def _envelope_key(version: str) -> bytes:
    """Resolve a server-only envelope KEK by non-secret version identifier."""
    if version in _envelope_key_cache:
        return _envelope_key_cache[version]
    value = os.getenv(f"ENCRYPTION_KEY_{version.upper()}", "")
    # Rollout compatibility: existing deployments have one server-only
    # ENCRYPTION_KEY. Treat it as the initial v1/v2 KEK until an explicit
    # ENCRYPTION_KEY_V2 is configured; new versions never inherit it.
    if not value and version in {"v1", "v2"}:
        value = os.getenv("ENCRYPTION_KEY", "")
        if not value:
            # Do not depend on creditpaper.db having been imported first merely
            # to load dotenv. Auth settings own this secret and load the active
            # environment file deterministically.
            from creditpaper.auth.config import get_settings

            value = get_settings().ENCRYPTION_KEY
    if not value:
        raise ValueError(f"Unknown encryption key version: {version}")
    try:
        key = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError("Invalid encryption key encoding") from exc
    if len(key) not in {16, 24, 32}:
        raise ValueError("Encryption key must be 128, 192, or 256 bits")
    _envelope_key_cache[version] = key
    return key


def _aad(tenant_id: str, record_id: str, purpose: str, version: str) -> bytes:
    try:
        tenant = str(uuid.UUID(str(tenant_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Envelope tenant binding must be a UUID") from exc
    if not record_id or len(record_id) > 200 or not purpose or len(purpose) > 120:
        raise ValueError("Envelope record and purpose are required and length-limited")
    return json.dumps(
        {"tenant_id": tenant, "record_id": record_id, "purpose": purpose, "version": version},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def encrypt_envelope(
    data: dict, *, tenant_id: str, record_id: str, purpose: str, key_version: str = "v2"
) -> str:
    """Encrypt with a random record DEK wrapped by a versioned server-only KEK.

    The DEK is never stored in plaintext. Both the wrapped DEK and payload are
    authenticated against tenant, record, purpose, and envelope version.
    ``key_version`` identifies only the KEK and is safe to persist.
    """
    envelope_version = "v3"
    aad = _aad(tenant_id, record_id, purpose, envelope_version)
    dek = AESGCM.generate_key(bit_length=256)
    payload_nonce = os.urandom(12)
    wrap_nonce = os.urandom(12)
    payload_ciphertext = AESGCM(dek).encrypt(
        payload_nonce, json.dumps(data, separators=(",", ":")).encode(), aad
    )
    wrapped_dek = AESGCM(_envelope_key(key_version)).encrypt(wrap_nonce, dek, aad + b":wrapped-dek")
    envelope = {
        "key_version": key_version,
        "wrap_nonce": base64.b64encode(wrap_nonce).decode(),
        "wrapped_dek": base64.b64encode(wrapped_dek).decode(),
        "payload_nonce": base64.b64encode(payload_nonce).decode(),
        "ciphertext": base64.b64encode(payload_ciphertext).decode(),
    }
    encoded = base64.b64encode(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()).decode()
    return f"{envelope_version}:{encoded}"


def decrypt_envelope(token: str, *, tenant_id: str, record_id: str, purpose: str) -> dict:
    """Decrypt only when version, tenant, record, and purpose all authenticate."""
    version, separator, encoded = token.partition(":")
    if not separator or version not in {"v2", "v3"} or not encoded:
        raise ValueError("Unknown or missing envelope version")
    try:
        if version == "v3":
            envelope = json.loads(base64.b64decode(encoded, validate=True))
            if not isinstance(envelope, dict) or set(envelope) != {
                "key_version",
                "wrap_nonce",
                "wrapped_dek",
                "payload_nonce",
                "ciphertext",
            }:
                raise ValueError("Malformed encrypted envelope")
            key_version = envelope["key_version"]
            if not isinstance(key_version, str) or not key_version:
                raise ValueError("Missing envelope key version")
            aad = _aad(tenant_id, record_id, purpose, version)
            wrap_nonce = base64.b64decode(envelope["wrap_nonce"], validate=True)
            wrapped_dek = base64.b64decode(envelope["wrapped_dek"], validate=True)
            payload_nonce = base64.b64decode(envelope["payload_nonce"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
            if len(wrap_nonce) != 12 or len(payload_nonce) != 12:
                raise ValueError("Invalid envelope nonce")
            dek = AESGCM(_envelope_key(key_version)).decrypt(wrap_nonce, wrapped_dek, aad + b":wrapped-dek")
            plaintext = AESGCM(dek).decrypt(payload_nonce, ciphertext, aad)
        else:
            # Bounded dual-read window for records written by the former v2
            # direct-KEK format. All new writes use v3.
            raw = base64.b64decode(encoded, validate=True)
            nonce, ciphertext = raw[:12], raw[12:]
            plaintext = AESGCM(_envelope_key(version)).decrypt(
                nonce, ciphertext, _aad(tenant_id, record_id, purpose, version)
            )
        value = json.loads(plaintext)
    except Exception as exc:
        raise ValueError("Invalid encrypted envelope") from exc
    if not isinstance(value, dict):
        raise ValueError("Encrypted envelope payload must be an object")
    return value


def rewrap_envelope(
    token: str,
    *,
    tenant_id: str,
    record_id: str,
    purpose: str,
    new_key_version: str,
) -> str:
    """Rewrap a v3 record DEK without decrypting its credential payload."""
    version, separator, encoded = token.partition(":")
    if not separator or version != "v3":
        raise ValueError("Only v3 envelopes can be rewrapped")
    try:
        envelope = json.loads(base64.b64decode(encoded, validate=True))
        aad = _aad(tenant_id, record_id, purpose, version)
        old_key_version = envelope["key_version"]
        old_nonce = base64.b64decode(envelope["wrap_nonce"], validate=True)
        wrapped_dek = base64.b64decode(envelope["wrapped_dek"], validate=True)
        dek = AESGCM(_envelope_key(old_key_version)).decrypt(old_nonce, wrapped_dek, aad + b":wrapped-dek")
        new_nonce = os.urandom(12)
        envelope["key_version"] = new_key_version
        envelope["wrap_nonce"] = base64.b64encode(new_nonce).decode()
        envelope["wrapped_dek"] = base64.b64encode(
            AESGCM(_envelope_key(new_key_version)).encrypt(new_nonce, dek, aad + b":wrapped-dek")
        ).decode()
        return (
            "v3:"
            + base64.b64encode(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()).decode()
        )
    except Exception as exc:
        raise ValueError("Invalid encrypted envelope") from exc
