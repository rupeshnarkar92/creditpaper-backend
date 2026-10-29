"""Tenant-scoped store for onboarding papers and their derived artefacts.

Postgres-backed, mirroring the table layout in `creditpaper.db.init_schema`.
Paper binaries (uploads) stay on disk via `storage_path`; only metadata and
derived artefacts move to the database.

`tenant_id` is validated on every path build, so a traversal attempt cannot
escape the tenant directory. Cross-tenant access is blocked at the SQL
boundary (every WHERE clause includes `tenant_id`).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from creditpaper.db import tenant_cursor, tenant_uuid
from creditpaper.paths import ensure_output_dir

_SAFE_TENANT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

_DEAL_WRITABLE_COLUMNS = frozenset(
    {
        "name",
        "deal_type",
        "borrower",
        "sector",
        "address",
        "thesis_content",
        "thesis_status",
    }
)

_EMPTY_ARTIFACTS: dict[str, Any] = {
    "sections": [],
    "facts": [],
    "tags": [],
    "style": None,
    "events": [],
    "extraction": None,
}


def _validate_tenant(tenant_id: str) -> str:
    if not tenant_id or not (_SAFE_TENANT.match(tenant_id) or _UUID_RE.match(tenant_id)):
        raise ValueError("invalid tenant_id")
    if ".." in tenant_id or "/" in tenant_id or "\\" in tenant_id:
        raise ValueError("invalid tenant_id")
    return tenant_id


def _validate_paper_id(paper_id: str) -> str:
    if not paper_id:
        raise ValueError("invalid paper_id")
    return paper_id


def _paper_dir(tenant_id: str, paper_id: str) -> Path:
    _validate_tenant(tenant_id)
    _validate_paper_id(paper_id)
    return ensure_output_dir("tenants", tenant_id, "onboarding", "papers", paper_id)


def _row_to_paper(row: Mapping[str, Any] | tuple, cols: Sequence[str] | None = None) -> dict[str, Any]:
    """Project a full-width deals row (SETOF deals) into the paper dict by column NAME.

    The live `deals` table has 24 columns and no `stage`. Never unpack positionally.
    """
    if cols is not None and not isinstance(row, Mapping):
        row = dict(zip(cols, row, strict=True))
    return {
        "id": str(row["id"]),
        "name": row.get("name"),
        "deal_type": row.get("deal_type"),
        "borrower": row.get("borrower"),
        "sector": row.get("sector"),
        "address": row.get("address"),
        "thesis_content": row.get("thesis_content"),
        "thesis_status": row.get("thesis_status"),
        "owner_id": str(row["owner_id"]) if row.get("owner_id") else None,
        "current_state": row.get("current_state"),
        "document_count": row.get("document_count"),
        "document_required": row.get("document_required"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "deleted_at": row["deleted_at"].isoformat() if row.get("deleted_at") else None,
    }


# --------------------------------------------------------------------------
# papers (index)
# --------------------------------------------------------------------------


def list_papers(tenant_id: str) -> list[dict[str, Any]]:
    _validate_tenant(tenant_id)
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT * FROM public.onboarding_papers(%s)", (tid,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    return [_row_to_paper(r) for r in rows]


def get_paper(tenant_id: str, paper_id: str) -> dict[str, Any] | None:
    _validate_paper_id(paper_id)
    _validate_tenant(tenant_id)
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT * FROM public.onboarding_paper(%s,%s)", (tid, paper_id))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return _row_to_paper(dict(zip(cols, row, strict=True)))


def new_paper_id() -> str:
    return str(uuid.uuid4())


def create_paper(tenant_id: str, record: dict[str, Any]) -> dict[str, Any]:
    _validate_tenant(tenant_id)
    tid = tenant_uuid(tenant_id)
    paper_id = record.get("id") or new_paper_id()
    for key in record:
        if key != "id" and key not in _DEAL_WRITABLE_COLUMNS:
            raise ValueError(f"unknown key: {key}")
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT * FROM public.create_onboarding_paper(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                paper_id,
                tid,
                record.get("name"),
                record.get("deal_type"),
                record.get("borrower"),
                record.get("sector"),
                record.get("address"),
                None,
                record.get("thesis_content"),
                record.get("thesis_status"),
            ),
        )
        row = cur.fetchone()
        if row is None:
            return {}
        cols = [d[0] for d in cur.description]
        return _row_to_paper(dict(zip(cols, row, strict=True)))


def update_paper(tenant_id: str, paper_id: str, **fields: Any) -> dict[str, Any] | None:
    """Patch a paper row in place. Returns the updated row, or None if absent."""
    _validate_paper_id(paper_id)
    _validate_tenant(tenant_id)
    tid = tenant_uuid(tenant_id)
    values: dict[str, Any] = {}
    for col, val in fields.items():
        if col not in _DEAL_WRITABLE_COLUMNS:
            raise ValueError(f"unknown column: {col}")
        values[col] = val
    if values:
        with tenant_cursor(str(tid)) as cur:
            cur.execute(
                "SELECT public.update_onboarding_paper(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    tid,
                    paper_id,
                    values.get("name"),
                    values.get("deal_type"),
                    values.get("borrower"),
                    values.get("sector"),
                    values.get("address"),
                    values.get("thesis_content"),
                    values.get("thesis_status"),
                ),
            )
            res = cur.fetchone()
            if not res or not res[0]:
                return None
    return get_paper(tenant_id, paper_id)


def delete_paper(tenant_id: str, paper_id: str) -> bool:
    """Remove the row and every derived artefact, including the stored upload."""
    _validate_paper_id(paper_id)
    _validate_tenant(tenant_id)
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.delete_onboarding_paper(%s,%s)", (tid, paper_id))
        removed = bool(cur.fetchone()[0])
    if removed:
        import shutil

        shutil.rmtree(_paper_dir(tenant_id, paper_id), ignore_errors=True)
    return removed


def storage_path(tenant_id: str, paper_id: str, filename: str) -> Path:
    """Tenant- and paper-scoped upload path. The original filename never becomes
    part of the directory structure — only its sanitised extension survives."""
    ext = Path(filename).suffix.lower()
    if ext not in {".pdf", ".docx"}:
        ext = ".bin"
    return _paper_dir(tenant_id, paper_id) / f"original{ext}"


# --------------------------------------------------------------------------
# derived artefacts: sections, facts, tags, style, extraction, events
# --------------------------------------------------------------------------


def _load_model(tenant_id: str, paper_id: str) -> dict[str, Any]:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.onboarding_model_snapshot(%s,%s)", (tid, paper_id))
        row = cur.fetchone()
    if not row:
        return {**_EMPTY_ARTIFACTS}
    return {**_EMPTY_ARTIFACTS, **(row[0] or {})}


def _save_model(tenant_id: str, paper_id: str, data: dict[str, Any]) -> None:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.save_onboarding_model(%s,%s,%s::jsonb)", (paper_id, tid, json.dumps(data)))


def _load_events(tenant_id: str, paper_id: str) -> list[dict[str, Any]]:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT * FROM public.onboarding_deal_events(%s,%s)", (tid, paper_id))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    out: list[dict[str, Any]] = []
    for row in rows:
        created = row.get("created_at")
        out.append(
            {
                "stage": row.get("stage"),
                "status": row.get("status"),
                "message": row.get("message"),
                "metadata": row.get("metadata") or {},
                "created_at": created.isoformat() if created else None,
            }
        )
    return out


def load_artifacts(tenant_id: str, paper_id: str) -> dict[str, Any]:
    _validate_tenant(tenant_id)
    _validate_paper_id(paper_id)
    data = _load_model(tenant_id, paper_id)
    data["events"] = _load_events(tenant_id, paper_id)
    return data


def replace_sections(tenant_id: str, paper_id: str, sections: list[dict[str, Any]]) -> None:
    """Replace, never append. A re-run of the stage must not double the rows —
    idempotency by construction rather than by de-duplicating afterwards."""
    _validate_tenant(tenant_id)
    _validate_paper_id(paper_id)
    data = _load_model(tenant_id, paper_id)
    data["sections"] = sections
    _save_model(tenant_id, paper_id, data)


def replace_facts(tenant_id: str, paper_id: str, facts: list[dict[str, Any]]) -> None:
    _validate_tenant(tenant_id)
    _validate_paper_id(paper_id)
    data = _load_model(tenant_id, paper_id)
    data["facts"] = facts
    _save_model(tenant_id, paper_id, data)


def replace_tags(tenant_id: str, paper_id: str, tags: list[dict[str, Any]]) -> None:
    _validate_tenant(tenant_id)
    _validate_paper_id(paper_id)
    data = _load_model(tenant_id, paper_id)
    data["tags"] = tags
    _save_model(tenant_id, paper_id, data)


def save_style(tenant_id: str, paper_id: str, style: dict[str, Any]) -> None:
    _validate_tenant(tenant_id)
    _validate_paper_id(paper_id)
    data = _load_model(tenant_id, paper_id)
    data["style"] = style
    _save_model(tenant_id, paper_id, data)


def save_extraction(tenant_id: str, paper_id: str, extraction: dict[str, Any]) -> None:
    """Structured page-level content. Kept out of the index so listing papers
    never loads document text."""
    _validate_tenant(tenant_id)
    _validate_paper_id(paper_id)
    data = _load_model(tenant_id, paper_id)
    data["extraction"] = extraction
    _save_model(tenant_id, paper_id, data)


def record_event(
    tenant_id: str,
    paper_id: str,
    stage: str,
    status: str,
    message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """One row per (deal_id, stage). A retry overwrites its own stage rather than
    appending a second history for the same step."""
    _validate_tenant(tenant_id)
    _validate_paper_id(paper_id)
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT public.record_onboarding_event(%s,%s,%s,%s,%s,%s::jsonb)",
            (paper_id, tid, stage, status, message, json.dumps(metadata or {})),
        )


# The three-paper gate is NOT computed here on purpose. This module is storage;
# the rule for what counts as "validated approved" lives beside the state machine
# in `services/onboarding/papers/gate.py`, so the store stays a dumb, tenant-safe
# persistence seam and the engine never depends on the service layer.
