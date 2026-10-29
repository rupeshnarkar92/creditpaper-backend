"""Restore a protected Windows development backup into a NEW isolated database.

Never overwrites the application database. Requires the Windows account that
created the DPAPI-protected key. Production uses its managed backup/KMS service.
"""

from __future__ import annotations

import argparse
import ctypes
import io
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from psycopg import sql


class Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def unprotect(value: bytes) -> bytes:
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise RuntimeError("Windows backup key cannot be decrypted by this account")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-prefix", type=Path, required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    if os.name != "nt" or not re.fullmatch(r"creditpaper_restore_[a-z0-9_]{1,32}", args.target):
        parser.error("Use Windows and a new creditpaper_restore_... database name")
    prefix = args.backup_prefix.resolve()
    cipher = Fernet(unprotect(prefix.with_suffix(".key.dpapi").read_bytes()))
    config = json.loads(cipher.decrypt(prefix.with_suffix(".environment.enc").read_bytes()))
    values = dotenv_values(stream=io.StringIO(config[".env.local"]))
    dsn = values["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    parsed = urlsplit(dsn)
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("This recovery tool only restores local development backups")
    with psycopg.connect(dsn, autocommit=True) as conn:
        # CREATE fails if the target already exists. No DROP or overwrite path.
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(args.target)))
    result = subprocess.run(
        [
            "C:/Program Files/PostgreSQL/18/bin/pg_restore.exe",
            "--host",
            parsed.hostname,
            "--port",
            str(parsed.port or 5432),
            "--username",
            parsed.username,
            "--dbname",
            args.target,
            "--no-owner",
            "--exit-on-error",
        ],
        input=cipher.decrypt(prefix.with_suffix(".dump.enc").read_bytes()),
        env=dict(os.environ, PGPASSWORD=parsed.password or ""),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise RuntimeError("Restore failed; the application database was not changed")
    print("Restored into the new isolated database. Validate it before any operator cutover.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Recovery stopped:", type(exc).__name__)
        raise SystemExit(1) from None
