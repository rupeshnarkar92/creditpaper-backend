"""One-time migration of file-based stores into Postgres.

Runs on first startup. If a row already exists in the target table, the
upsert is a no-op. If a file is missing, the loop continues.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from creditpaper.db import cursor, tenant_uuid
from creditpaper.onboarding.utils.store import _validate_tenant

_OUTPUT_ROOT = Path(os.getenv("OUTPUT_DIR", "output"))
_MIGRATED_DIR_NAME = ".migrated"


def _migrated_marker() -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return _OUTPUT_ROOT / "tenants" / f".migrated-{ts}"


def run_one_time_migration() -> None:
    """Idempotent. Reads output/tenants/{id}/*.json once, upserts into Postgres,
    moves the source files into a .migrated-{ts}/ directory."""
    tenants_dir = _OUTPUT_ROOT / "tenants"
    if not tenants_dir.exists():
        return

    # Check if a previous migration has already run. The marker is a sibling
    # directory; the presence of any .migrated-{ts}/ dir means we are done.
    if any(tenants_dir.glob(".migrated-*")):
        return

    migrated_dir = _migrated_marker()
    migrated_dir.mkdir(parents=True, exist_ok=True)

    for tenant_subdir in tenants_dir.iterdir():
        if not tenant_subdir.is_dir() or tenant_subdir.name.startswith("."):
            continue
        tenant_id = tenant_subdir.name
        try:
            _validate_tenant(tenant_id)
        except ValueError:
            continue
        _migrate_one_tenant(tenant_id, tenant_subdir, migrated_dir)

    # Ensure the canonical tenant "1" exists.
    tid = tenant_uuid("1")
    with cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (tid, "Default Tenant"),
        )


def _migrate_one_tenant(tenant_id: str, source_dir: Path, migrated_dir: Path) -> None:
    tid = tenant_uuid(tenant_id)

    # brand_profile.json
    brand = source_dir / "brand_profile.json"
    if brand.exists():
        try:
            payload = json.loads(brand.read_text(encoding="utf-8"))
            with cursor() as cur:
                cur.execute(
                    "INSERT INTO brand_profile (tenant_id, profile) VALUES (%s, %s) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET profile = EXCLUDED.profile",
                    (tid, json.dumps(payload)),
                )
        except Exception:
            pass

    # voice_profile.json
    voice = source_dir / "voice_profile.json"
    if voice.exists():
        try:
            payload = json.loads(voice.read_text(encoding="utf-8"))
            with cursor() as cur:
                cur.execute(
                    "INSERT INTO voice_profile (tenant_id, profile) VALUES (%s, %s) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET profile = EXCLUDED.profile",
                    (tid, json.dumps(payload)),
                )
        except Exception:
            pass

    # onboarding.json
    state = source_dir / "onboarding.json"
    if state.exists():
        try:
            payload = json.loads(state.read_text(encoding="utf-8"))
            with cursor() as cur:
                cur.execute(
                    "INSERT INTO onboarding_state (tenant_id, state) VALUES (%s, %s) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET state = EXCLUDED.state",
                    (tid, json.dumps(payload)),
                )
        except Exception:
            pass

    # onboarding_documents.json
    docs = source_dir / "onboarding_documents.json"
    if docs.exists():
        try:
            payload = json.loads(docs.read_text(encoding="utf-8"))
            for d in payload:
                with cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO onboarding_documents (
                            id, tenant_id, filename, status, is_credit_paper,
                            approval_detected, confidence, reason, size,
                            content_type, tags, storage_path
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (tenant_id, filename) DO UPDATE
                        SET status = EXCLUDED.status
                        """,
                        (
                            d.get("id") or "00000000-0000-0000-0000-000000000000",
                            tid,
                            d["filename"],
                            d["status"],
                            d.get("is_credit_paper", False),
                            d.get("approval_detected", False),
                            d.get("confidence", 0.0),
                            d.get("reason"),
                            d.get("size", 0),
                            d.get("content_type"),
                            json.dumps(d.get("tags") or []),
                            d.get("storage_path", ""),
                        ),
                    )
        except Exception:
            pass

    # section_order.json
    order = source_dir / "section_order.json"
    if order.exists():
        try:
            payload = json.loads(order.read_text(encoding="utf-8"))
            with cursor() as cur:
                cur.execute(
                    "INSERT INTO section_orders (tenant_id, sections) VALUES (%s, %s) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET sections = EXCLUDED.sections",
                    (tid, json.dumps(payload)),
                )
        except Exception:
            pass

    # Move the tenant dir contents into the migrated directory.
    dest = migrated_dir / tenant_id
    dest.mkdir(parents=True, exist_ok=True)
    for f in source_dir.iterdir():
        if f.is_file():
            shutil.move(str(f), str(dest / f.name))
