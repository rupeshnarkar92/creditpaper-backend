"""Server-only encryption-at-rest for uploaded document binaries.

New writes use a random per-document DEK wrapped by the configured versioned
KEK. Reads retain a bounded plaintext fallback for files created before this
format existed. Callers must supply the authoritative tenant and record IDs;
moving ciphertext between tenants or records fails AEAD authentication.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from creditpaper.auth.security.encryption import _aad, _envelope_key

MAGIC = b"CPDOC3\n"
STREAM_MAGIC = b"CPDOC4\n"
PURPOSE = "stored_document"
_STREAM_CHUNK = 1024 * 1024


def encrypt_bytes(data: bytes, *, tenant_id: str, record_id: str, key_version: str = "v2") -> bytes:
    aad = _aad(tenant_id, record_id, PURPOSE, "v3")
    dek = AESGCM.generate_key(bit_length=256)
    payload_nonce, wrap_nonce = os.urandom(12), os.urandom(12)
    envelope = {
        "key_version": key_version,
        "payload_nonce": base64.b64encode(payload_nonce).decode(),
        "ciphertext": base64.b64encode(AESGCM(dek).encrypt(payload_nonce, data, aad)).decode(),
        "wrap_nonce": base64.b64encode(wrap_nonce).decode(),
        "wrapped_dek": base64.b64encode(
            AESGCM(_envelope_key(key_version)).encrypt(wrap_nonce, dek, aad + b":wrapped-dek")
        ).decode(),
    }
    return MAGIC + json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()


def decrypt_bytes(blob: bytes, *, tenant_id: str, record_id: str) -> bytes:
    if blob.startswith(STREAM_MAGIC):
        source = tempfile.NamedTemporaryFile(delete=False)
        target = tempfile.NamedTemporaryFile(delete=False)
        source_path, target_path = Path(source.name), Path(target.name)
        try:
            with source:
                source.write(blob)
            target.close()
            try:
                _decrypt_stream_to_path(source_path, target_path, tenant_id=tenant_id, record_id=record_id)
                return target_path.read_bytes()
            except Exception as exc:
                raise ValueError("Invalid encrypted document") from exc
        finally:
            source_path.unlink(missing_ok=True)
            target_path.unlink(missing_ok=True)
    if not blob.startswith(MAGIC):
        return blob  # legacy plaintext, rewritten encrypted on the next update
    try:
        envelope = json.loads(blob[len(MAGIC) :])
        if not isinstance(envelope, dict) or set(envelope) != {
            "key_version",
            "payload_nonce",
            "ciphertext",
            "wrap_nonce",
            "wrapped_dek",
        }:
            raise ValueError("Malformed document envelope")
        aad = _aad(tenant_id, record_id, PURPOSE, "v3")
        wrap_nonce = base64.b64decode(envelope["wrap_nonce"], validate=True)
        payload_nonce = base64.b64decode(envelope["payload_nonce"], validate=True)
        if len(wrap_nonce) != 12 or len(payload_nonce) != 12:
            raise ValueError("Invalid document nonce")
        dek = AESGCM(_envelope_key(envelope["key_version"])).decrypt(
            wrap_nonce,
            base64.b64decode(envelope["wrapped_dek"], validate=True),
            aad + b":wrapped-dek",
        )
        return AESGCM(dek).decrypt(
            payload_nonce, base64.b64decode(envelope["ciphertext"], validate=True), aad
        )
    except Exception as exc:
        raise ValueError("Invalid encrypted document") from exc


def write_document(path: Path, data: bytes, *, tenant_id: str, record_id: str) -> None:
    """Atomically replace one encrypted document with a mode-0600 envelope."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encrypted = encrypt_bytes(data, tenant_id=tenant_id, record_id=record_id)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_document_from_path(
    path: Path, source: Path, *, tenant_id: str, record_id: str, key_version: str = "v2"
) -> None:
    """Encrypt a large file atomically without holding plaintext or ciphertext in memory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    aad = _aad(tenant_id, record_id, PURPOSE, "v4")
    dek = AESGCM.generate_key(bit_length=256)
    payload_nonce, wrap_nonce = os.urandom(12), os.urandom(12)
    header = json.dumps(
        {
            "key_version": key_version,
            "payload_nonce": base64.b64encode(payload_nonce).decode(),
            "wrap_nonce": base64.b64encode(wrap_nonce).decode(),
            "wrapped_dek": base64.b64encode(
                AESGCM(_envelope_key(key_version)).encrypt(wrap_nonce, dek, aad + b":wrapped-dek")
            ).decode(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(STREAM_MAGIC)
            output.write(struct.pack(">I", len(header)))
            output.write(header)
            encryptor = Cipher(algorithms.AES(dek), modes.GCM(payload_nonce)).encryptor()
            encryptor.authenticate_additional_data(aad)
            with source.open("rb") as input_file:
                while chunk := input_file.read(_STREAM_CHUNK):
                    output.write(encryptor.update(chunk))
            output.write(encryptor.finalize())
            output.write(encryptor.tag)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _decrypt_stream_to_path(source: Path, target: Path, *, tenant_id: str, record_id: str) -> None:
    aad = _aad(tenant_id, record_id, PURPOSE, "v4")
    with source.open("rb") as input_file:
        if input_file.read(len(STREAM_MAGIC)) != STREAM_MAGIC:
            raise ValueError("Invalid streaming document envelope")
        header_size_raw = input_file.read(4)
        if len(header_size_raw) != 4:
            raise ValueError("Malformed streaming document envelope")
        header_size = struct.unpack(">I", header_size_raw)[0]
        if header_size < 32 or header_size > 16 * 1024:
            raise ValueError("Invalid streaming document header")
        envelope = json.loads(input_file.read(header_size))
        payload_nonce = base64.b64decode(envelope["payload_nonce"], validate=True)
        wrap_nonce = base64.b64decode(envelope["wrap_nonce"], validate=True)
        dek = AESGCM(_envelope_key(envelope["key_version"])).decrypt(
            wrap_nonce,
            base64.b64decode(envelope["wrapped_dek"], validate=True),
            aad + b":wrapped-dek",
        )
        ciphertext_start = input_file.tell()
        input_file.seek(0, os.SEEK_END)
        ciphertext_size = input_file.tell() - ciphertext_start - 16
        if ciphertext_size < 0:
            raise ValueError("Truncated streaming document envelope")
        input_file.seek(-16, os.SEEK_END)
        tag = input_file.read(16)
        input_file.seek(ciphertext_start)
        decryptor = Cipher(algorithms.AES(dek), modes.GCM(payload_nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(aad)
        remaining = ciphertext_size
        with target.open("wb") as output:
            while remaining:
                chunk = input_file.read(min(_STREAM_CHUNK, remaining))
                if not chunk:
                    raise ValueError("Truncated streaming document ciphertext")
                remaining -= len(chunk)
                output.write(decryptor.update(chunk))
            output.write(decryptor.finalize())


def read_document(path: Path, *, tenant_id: str, record_id: str) -> bytes:
    return decrypt_bytes(path.read_bytes(), tenant_id=tenant_id, record_id=record_id)


@contextmanager
def materialize_document(
    path: Path, *, tenant_id: str, record_id: str, suffix: str | None = None
) -> Iterator[Path]:
    """Provide a short-lived plaintext file to path-only parsers."""
    handle = tempfile.NamedTemporaryFile(suffix=suffix or path.suffix, delete=False)
    temporary = Path(handle.name)
    try:
        handle.close()
        with path.open("rb") as source:
            magic = source.read(len(STREAM_MAGIC))
        if magic == STREAM_MAGIC:
            _decrypt_stream_to_path(path, temporary, tenant_id=tenant_id, record_id=record_id)
        else:
            temporary.write_bytes(read_document(path, tenant_id=tenant_id, record_id=record_id))
        os.chmod(temporary, 0o600)
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)
