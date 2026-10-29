"""Raw psycopg queries for the document slot board (contract 2026-09-15 §A).

Every query that reads or writes a deal's slots, files or documents is scoped by tenant.
The service calls these; tests replace this module's functions with an in-memory fake.
Slot types and refused classes are platform rows (migration 0015), not tenant data.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from creditpaper.db import read_cursor, tenant_cursor, tenant_uuid

log = logging.getLogger("uvicorn.error")


def bundle_inputs(tenant_id: str, version_ids: list[str], known: dict[str, str]) -> list:
    """Revalidate cached findings; fetch complete pages only for changed versions."""
    if not version_ids:
        return []
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT * FROM public.slot_bundle_inputs(%s,%s::uuid[],%s::jsonb)",
            (tenant_uuid(tenant_id), sorted(set(version_ids)), json.dumps(known)),
        )
        return [(str(v), fp, pages) for v, fp, pages in cur.fetchall()]


def match_pages(tenant_id: str, deal_id: str, version_ids: list[str]) -> dict:
    if not version_ids:
        return {}
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT * FROM public.slot_match_pages(%s,%s,%s::uuid[])",
            (tenant_uuid(tenant_id), deal_id, list(dict.fromkeys(version_ids))),
        )
        return {str(version): {int(p["page"]): p["text"] for p in pages} for version, pages in cur.fetchall()}


def match_inputs(tenant_id: str, deal_id: str, version_ids: list[str]) -> dict:
    if not version_ids:
        return {}
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT * FROM public.slot_match_inputs(%s,%s,%s::uuid[])",
            (tenant_uuid(tenant_id), deal_id, list(dict.fromkeys(version_ids))),
        )
        return {str(row[0]): (str(row[1]) if row[1] else None, str(row[2] or "")) for row in cur.fetchall()}


# --- stored procedure helper ------------------------------------------------------


def call_slot_board(tenant_id: str, deal_id: str) -> dict[str, Any]:
    """One atomic autocommit statement, including lazy slot creation."""
    started = time.perf_counter()
    with tenant_cursor(tenant_id) as cur:
        checked_out = time.perf_counter()
        cur.execute("SELECT public.get_slot_board(%s, %s)", (tenant_uuid(tenant_id), deal_id))
        row = cur.fetchone()
    queried = time.perf_counter()
    if not row or not row[0]:
        log.info(
            "slot_board_db checkout_s=%.3f query_s=%.3f decode_s=0.000 total_s=%.3f found=false",
            checked_out - started,
            queried - checked_out,
            queried - started,
        )
        return {}
    result = row[0]
    if isinstance(result, str):
        result = json.loads(result)
    finished = time.perf_counter()
    log.info(
        "slot_board_db checkout_s=%.3f query_s=%.3f decode_s=%.3f total_s=%.3f found=true",
        checked_out - started,
        queried - checked_out,
        finished - queried,
        finished - started,
    )
    return result


def call_fact_sheet_data(tenant_id: str, deal_id: str) -> dict[str, Any]:
    """Call the get_fact_sheet_data() stored procedure. Returns the raw JSONB payload."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT public.get_fact_sheet_data(%s, %s)", (tenant_uuid(tenant_id), deal_id))
        row = cur.fetchone()
    if not row or not row[0]:
        return {}
    result = row[0]
    if isinstance(result, str):
        result = json.loads(result)
    return result


def call_workspace_page(tenant_id: str, deal_id: str) -> dict[str, Any]:
    """Call the get_workspace_page() mega-SP. Returns all workspace page data in one round-trip."""
    started = time.perf_counter()
    with tenant_cursor(tenant_id) as cur:
        checked_out = time.perf_counter()
        cur.execute("SELECT public.get_workspace_page(%s, %s)", (tenant_uuid(tenant_id), deal_id))
        row = cur.fetchone()
    queried = time.perf_counter()
    if not row or not row[0]:
        log.info(
            "workspace_page_db checkout_s=%.3f query_s=%.3f decode_s=0.000 total_s=%.3f found=false",
            checked_out - started,
            queried - checked_out,
            queried - started,
        )
        return {}
    result = row[0]
    if isinstance(result, str):
        result = json.loads(result)
    finished = time.perf_counter()
    log.info(
        "workspace_page_db checkout_s=%.3f query_s=%.3f decode_s=%.3f total_s=%.3f found=true",
        checked_out - started,
        queried - checked_out,
        finished - queried,
        finished - started,
    )
    return result


# --- platform rows -----------------------------------------------------------------


def slot_types() -> list[dict[str, Any]]:
    with read_cursor() as cur:
        cur.execute("SELECT * FROM public.platform_slot_types()")
        rows = cur.fetchall()
    return [
        {
            "key": r[0],
            "name": r[1],
            "description": r[2],
            "category": r[3],
            "applies_to": list(r[4] or []),
            "optional_for": list(r[5] or []),
            "multi": bool(r[6]),
            "extractor": r[7],
            "model_block": r[8],
            "checks": list(r[9] or []),
            "sort_order": int(r[10]),
        }
        for r in rows
    ]


def refused_classes() -> list[dict[str, Any]]:
    with read_cursor() as cur:
        cur.execute("SELECT * FROM public.platform_refused_classes()")
        rows = cur.fetchall()
    return [
        {"key": r[0], "name": r[1], "reason": r[2], "match": list(r[3] or []), "decision": r[4]} for r in rows
    ]


# --- deal reads --------------------------------------------------------------------


def get_deal(tenant_id: str, deal_id: str) -> dict[str, Any] | None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.slot_deal_row(%s,%s)", (tenant_uuid(tenant_id), deal_id))
        row = cur.fetchone()
    return {"id": row[0], "deal_type": row[1]} if row else None


def ensure_slots(tenant_id: str, deal_id: str, slot_keys: list[str]) -> None:
    """Create the deal's missing slot rows. Existing rows are never touched."""
    if not slot_keys:
        return
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.ensure_document_slots(%s,%s,%s)",
            (tenant_uuid(tenant_id), deal_id, slot_keys),
        )


def list_slots(tenant_id: str, deal_id: str) -> list[dict[str, Any]]:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT * FROM public.document_slot_rows(%s,%s)",
            (tenant_uuid(tenant_id), deal_id),
        )
        rows = cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "slot_key": r[1],
            "status": r[2],
            "confidence": float(r[3]) if r[3] is not None else None,
            "match_reason": r[4],
            "error": r[5],
            "updated_at": r[6],
        }
        for r in rows
    ]


def list_slot_files(tenant_id: str, deal_id: str) -> list[dict[str, Any]]:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT * FROM public.document_slot_file_rows(%s,%s)",
            (tenant_uuid(tenant_id), deal_id),
        )
        rows = cur.fetchall()
    return [
        {
            "slot_id": str(r[0]),
            "document_id": str(r[1]),
            "version_id": str(r[2]),
            "role": r[3],
            "confidence": float(r[4]) if r[4] is not None else None,
            "match_reason": r[5],
        }
        for r in rows
    ]


def list_documents(tenant_id: str, deal_id: str) -> list[dict[str, Any]]:
    """The deal's active (not archived) documents, each with every version, newest first."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT * FROM public.slot_document_versions(%s,%s)",
            (tenant_uuid(tenant_id), deal_id),
        )
        rows = cur.fetchall()
    docs: dict[str, dict[str, Any]] = {}
    for r in rows:
        doc = docs.setdefault(
            str(r[0]),
            {
                "document_id": str(r[0]),
                "display_name": r[1],
                "document_kind": r[2],
                "created_at": r[3],
                "versions": [],
            },
        )
        doc["versions"].append(
            {
                "version_id": str(r[4]),
                "version_number": int(r[5]),
                "original_filename": r[6],
                "size_bytes": int(r[7] or 0),
                "uploaded_at": r[8],
                "processing_status": r[9],
                "extraction_status": r[10],
                "extraction_error": r[11],
                "superseded_at": r[12],
            }
        )
    return list(docs.values())


def load_model(tenant_id: str, deal_id: str) -> dict[str, Any] | None:
    """The deal's current model snapshot as stored JSON (read-only)."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT public.slot_snapshot(%s,%s)", (tenant_uuid(tenant_id), deal_id))
        row = cur.fetchone()
    if not row:
        return None
    model = row[0]
    if isinstance(model, str):
        model = json.loads(model)
    return model if isinstance(model, dict) else None


def confirmed_slot_keys_by_deal(tenant_id: str, deal_ids: list[str]) -> dict[str, set[str]]:
    """Per deal, the slot keys the board shows as `confirmed`: stored confirmed, or stored
    processing with every assigned file's extraction ready. Only slots holding an active file."""
    if not deal_ids:
        return {}
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.confirmed_slot_keys(%s,%s)", (tid, deal_ids))
        rows = cur.fetchall()
    out: dict[str, set[str]] = {}
    for deal_id, key in rows:
        out.setdefault(deal_id, set()).add(key)
    return out


def version_processing_status(tenant_id: str, version_id: str) -> str | None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT public.version_processing_status(%s,%s)", (tenant_uuid(tenant_id), version_id))
        row = cur.fetchone()
    return row[0] if row else None


def set_extraction_status(tenant_id: str, version_id: str, status: str, error: str | None) -> None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.set_version_extraction_status(%s,%s,%s,%s)",
            (tenant_uuid(tenant_id), version_id, status, error),
        )


# --- the one write path for board mutations ----------------------------------------


def write_changes(
    *,
    tenant_id: str,
    deal_id: str,
    actor_id: str | None,
    slot_updates: list[dict[str, Any]] | None = None,
    add_files: list[dict[str, Any]] | None = None,
    remove_files: list[dict[str, Any]] | None = None,
    reset_versions: list[str] | None = None,
    archive_document: str | None = None,
    audit: list[dict[str, Any]] | None = None,
    confirmation: dict[str, Any] | None = None,
    extraction_jobs: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Apply one board mutation in a single transaction.

    slot_updates: {id, status, confidence, match_reason, error}
    add_files:    {slot_id, document_id, version_id, role, confidence?, match_reason?} (upsert)
    remove_files: {document_id, slot_id?, role?} (absent keys match any)
    reset_versions: version ids whose extraction is about to be re-run (-> processing)
    archive_document: a document to soft-delete (withdraw)
    audit:        {action, entity_type, entity_id, metadata}
    confirmation: expected proposed slot ID and assigned document/version pairs.
    extraction_jobs: jobs committed atomically with a confirmation; returns job IDs.
    """
    tid = tenant_uuid(tenant_id)
    now = datetime.now(UTC)
    with tenant_cursor(tenant_id) as cur:
        with cur.connection.transaction():
            cur.execute(
                (
                    "SELECT public.write_slot_confirmation(%s,%s,%s,%s::jsonb,%s)"
                    if confirmation is not None
                    else "SELECT public.write_slot_changes(%s,%s,%s,%s::jsonb,%s)"
                ),
                (
                    tid,
                    deal_id,
                    actor_id,
                    json.dumps(
                        {
                            "remove_files": remove_files or [],
                            "add_files": add_files or [],
                            "slot_updates": slot_updates or [],
                            "reset_versions": reset_versions or [],
                            "archive_document": archive_document,
                            "audit": audit or [],
                            "confirmation": confirmation,
                            "extraction_jobs": extraction_jobs or [],
                        },
                        default=str,
                    ),
                    now,
                ),
            )
            return [str(j) for j in cur.fetchone()[0]] if confirmation is not None else []
