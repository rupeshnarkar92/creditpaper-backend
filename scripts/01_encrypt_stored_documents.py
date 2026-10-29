"""Encrypt legacy plaintext document files in place.

Dry-run is the default. Run on the application host with the server-only
encryption keyring configured; database rows may contain host-local paths.
Only files whose metadata supplies an authoritative tenant and record ID are
eligible. No filenames, document contents, or keys are logged.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from creditpaper.db import read_cursor
from creditpaper.security.document_storage import MAGIC, encrypt_bytes


def _rows() -> list[tuple[str, str, str]]:
    with read_cursor() as cur:
        cur.execute(
            """SELECT tenant_id::text, id::text, storage_path
                 FROM public.deal_document_versions
                WHERE storage_path IS NOT NULL
               UNION ALL
               SELECT tenant_id::text, id::text, storage_path
                 FROM public.onboarding_documents
                WHERE storage_path IS NOT NULL"""
        )
        return [(str(t), str(r), str(p)) for t, r, p in cur.fetchall()]


def migrate(*, apply: bool) -> tuple[int, int, int]:
    eligible = encrypted = missing = 0
    for tenant_id, record_id, raw_path in _rows():
        path = Path(raw_path)
        if not path.is_file():
            missing += 1
            continue
        blob = path.read_bytes()
        if blob.startswith(MAGIC):
            encrypted += 1
            continue
        eligible += 1
        if not apply:
            continue
        replacement = encrypt_bytes(blob, tenant_id=tenant_id, record_id=record_id)
        handle, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(replacement)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            encrypted += 1
        finally:
            temporary.unlink(missing_ok=True)
    return eligible, encrypted, missing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="encrypt eligible files in place")
    args = parser.parse_args()
    eligible, encrypted, missing = migrate(apply=args.apply)
    mode = "applied" if args.apply else "dry-run"
    print(f"mode={mode} eligible={eligible} encrypted_or_already={encrypted} missing={missing}")


if __name__ == "__main__":
    main()
