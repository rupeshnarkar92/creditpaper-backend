"""Reproducible authenticated folder transfer for queue benchmarks.

Credentials are read only from BENCHMARK_EMAIL/BENCHMARK_PASSWORD. Tokens are
never printed or written. This measures archive construction and HTTP transfer;
durable processing is measured from PostgreSQL job timestamps separately.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import uuid
import zipfile
from pathlib import Path

import httpx
from dotenv import dotenv_values

SUPPORTED = {".pdf", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".docx", ".xlsx"}
MAX_BATCH_BYTES = 64 * 1024 * 1024


def _batches(root: Path) -> list[list[Path]]:
    files = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED]
    batches: list[list[Path]] = [[]]
    size = 0
    for path in files:
        file_size = path.stat().st_size
        if file_size > MAX_BATCH_BYTES:
            raise ValueError(f"file exceeds 64 MB: {path.relative_to(root)}")
        if batches[-1] and size + file_size > MAX_BATCH_BYTES:
            batches.append([])
            size = 0
        batches[-1].append(path)
        size += file_size
    return batches


def _archive(root: Path, files: list[Path]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in files:
            archive.write(path, path.relative_to(root).as_posix())
    return output.getvalue()


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: benchmark_folder_upload.py DEAL_ID FOLDER")
    deal_id, folder = sys.argv[1], Path(sys.argv[2]).resolve()
    email = os.environ["BENCHMARK_EMAIL"]
    password = os.environ["BENCHMARK_PASSWORD"]
    env = dotenv_values(Path(__file__).parents[1] / ".env")
    supabase_url = str(env["VITE_SUPABASE_URL"]).rstrip("/")
    anon_key = str(env["VITE_SUPABASE_ANON_KEY"])
    api_url = os.getenv("BENCHMARK_API_URL", "http://127.0.0.1:8000").rstrip("/")

    started = time.perf_counter()
    batches = _batches(folder)
    with httpx.Client(timeout=600) as client:
        auth = client.post(
            f"{supabase_url}/auth/v1/token?grant_type=password",
            headers={"apikey": anon_key},
            json={"email": email, "password": password},
        )
        auth.raise_for_status()
        token = auth.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        session = client.post(f"{api_url}/api/auth/session", headers=headers)
        session.raise_for_status()
        registered_at = time.perf_counter()

        ids = [str(uuid.uuid4()) for _ in batches]
        for batch_id, files in zip(ids, batches, strict=True):
            response = client.post(
                f"{api_url}/api/deals/{deal_id}/upload-batches",
                headers=headers,
                json={"id": batch_id, "total": len(files), "failed": 0, "sealed": False},
            )
            response.raise_for_status()

        built_s = transfer_s = 0.0
        for batch_id, files in zip(ids, batches, strict=True):
            tick = time.perf_counter()
            body = _archive(folder, files)
            built_s += time.perf_counter() - tick
            tick = time.perf_counter()
            response = client.post(
                f"{api_url}/api/deals/{deal_id}/upload-batches/{batch_id}/archive",
                headers=headers,
                files={"file": ("documents.zip", body, "application/zip")},
            )
            response.raise_for_status()
            transfer_s += time.perf_counter() - tick

    print(
        json.dumps(
            {
                "deal_id": deal_id,
                "supported_files": sum(map(len, batches)),
                "batches": len(batches),
                "session_s": round(registered_at - started, 3),
                "archive_build_s": round(built_s, 3),
                "http_transfer_s": round(transfer_s, 3),
                "client_total_s": round(time.perf_counter() - started, 3),
            }
        )
    )


if __name__ == "__main__":
    main()
