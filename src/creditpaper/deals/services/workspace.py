"""Workspace service layer.

Holds the read and mutation logic for the Deal Workspace slice 1. Lives
in the domain layer; the FastAPI routes in `routes/workspace.py` are
thin wrappers around these functions.

All mutating calls open a single transaction that updates the `deals`
row, inserts one `deal_state_transitions` row, and inserts one
`deal_audit_events` row. Any failure rolls back all three.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from creditpaper.db import cursor
from creditpaper.deals import state_machine
from creditpaper.deals.services.activity import record_deal_event, record_deal_event_best_effort
from creditpaper.obs.timing import timed

if TYPE_CHECKING:
    from creditpaper.deals.utils.ingest import IngestReport


class DealNotFound(Exception):
    """Raised when the deal does not exist in the caller's tenant.

    Translated to 404 DEAL_NOT_FOUND by the route layer. Cross-tenant
    access is intentionally indistinguishable from a deal that does
    not exist at all, to avoid leaking existence.
    """


class DealInvalidTransition(Exception):
    """Raised when (from_state, to_state) is not in the legal table."""

    def __init__(self, from_state: str, to_state: str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"cannot transition {from_state} -> {to_state}")


class DealRevisionMismatch(Exception):
    """Raised when expected_revision does not match the current revision."""

    def __init__(self, expected: int | None, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"workspace revision mismatch (expected {expected}, actual {actual})")


def _row_to_workspace_sp(row) -> dict:
    """Build workspace payload from stored procedure JSONB deal row."""
    return {
        "deal_id": str(row.get("id")),
        "tenant_id": str(row.get("tenant_id")),
        "name": row.get("name"),
        "borrower": row.get("borrower"),
        "deal_type": row.get("deal_type"),
        "current_state": row.get("current_state"),
        "workspace_revision": row.get("workspace_revision"),
        "state_changed_at": row.get("state_changed_at"),
        "state_changed_by": str(row["state_changed_by"]) if row.get("state_changed_by") else None,
        "facility_amount": float(row["facility_amount"]) if row.get("facility_amount") is not None else None,
        "status": row.get("status"),
        "updated_at": row.get("updated_at"),
    }


def workspace_from_raw(raw: dict) -> dict:
    """Pure workspace response builder for a stored-procedure payload."""
    if not raw or raw.get("deal") is None:
        raise DealNotFound("")
    payload = _row_to_workspace_sp(raw["deal"])
    payload["allowed_transitions"] = state_machine.allowed_from(payload["current_state"])
    payload["documents"] = _assemble_docs(raw.get("documents", []))
    from creditpaper.deals.services.activity_presenter import present

    payload["recent_activity"] = [
        {
            **item,
            **{
                k: v
                for k, v in present(item, raw.get("names", {})).items()
                if k in ("label", "actor", "tone", "category")
            },
        }
        for item in raw.get("audit", [])
    ]
    return payload


def _assemble_docs(docs_raw: list) -> list[dict]:
    """Assemble documents list from stored procedure JSONB."""
    docs = []
    for d in docs_raw:
        versions = []
        for v in d.get("versions", []):
            versions.append(
                {
                    "id": str(v.get("id")),
                    "version_number": v.get("version_number"),
                    "original_filename": v.get("original_filename"),
                    "mime_type": v.get("mime_type"),
                    "size_bytes": int(v.get("size_bytes", 0)),
                    "content_sha256": v.get("content_sha256"),
                    "uploaded_by": str(v.get("uploaded_by")),
                    "uploaded_at": v.get("uploaded_at"),
                    "processing_status": v.get("processing_status"),
                    "extraction_status": v.get("extraction_status"),
                    "extraction_error": v.get("extraction_error"),
                }
            )
        docs.append(
            {
                "id": str(d.get("id")),
                "display_name": d.get("display_name"),
                "document_kind": d.get("document_kind"),
                "created_at": d.get("created_at"),
                "version_count": len(versions),
                "versions": versions,
            }
        )
    return docs


def _call_workspace_sp(tenant_id: str, deal_id: str) -> dict:
    """Call the get_workspace_payload() stored procedure. Returns raw JSONB."""
    from creditpaper.db import read_cursor as _rc
    from creditpaper.db import tenant_uuid as _tid

    with _rc() as cur:
        cur.execute("SELECT get_workspace_payload(%s, %s)", (_tid(tenant_id), deal_id))
        row = cur.fetchone()
    if not row or not row[0]:
        return {}
    result = row[0]
    if isinstance(result, str):
        import json

        result = json.loads(result)
    return result


def get_workspace_payload(*, tenant_id: str, deal_id: str) -> dict:
    raw = _call_workspace_sp(tenant_id, deal_id)
    if not raw or raw.get("deal") is None:
        raise DealNotFound(deal_id)
    return workspace_from_raw(raw)


def with_presented_activity(tenant_id: str, items: list[dict]) -> list[dict]:
    """Additive: each recent_activity item gains `label`, `actor`, `tone` and `category` from the
    Activity presenter. Existing keys (including the original `created_at`) are unchanged."""
    from creditpaper.deals.services.activity import present_rows

    presented = present_rows(tenant_id, items)
    return [
        {**item, **{k: p[k] for k in ("label", "actor", "tone", "category")}}
        for item, p in zip(items, presented, strict=True)
    ]


def transition(
    *,
    tenant_id: str,
    deal_id: str,
    actor_id: str,
    to: str,
    reason: str | None,
    expected_revision: int | None,
    audit_metadata: dict | None = None,
) -> dict:
    """`audit_metadata` is merged into the audit event (approval records the paper
    theme version and template reference there)."""
    correlation_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    with cursor() as cur:
        cur.execute(
            "SELECT current_state, workspace_revision FROM deals WHERE id = %s AND tenant_id = %s FOR UPDATE",
            (deal_id, tenant_id),
        )
        row = cur.fetchone()
        if not row:
            raise DealNotFound(deal_id)
        from_state, current_revision = row[0], int(row[1])

        if expected_revision is not None and expected_revision != current_revision:
            raise DealRevisionMismatch(expected_revision, current_revision)
        if not state_machine.is_legal(from_state, to):
            raise DealInvalidTransition(from_state, to)
        if to == "APPROVED":
            from creditpaper.ledger.service import freeze_approval

            cur.execute("SELECT set_config('app.tenant_id',%s,true)", (str(tenant_id),))
            freeze_approval(cur, str(tenant_id), deal_id, str(actor_id))

        new_revision = current_revision + 1

        if expected_revision is not None:
            cur.execute(
                """
                UPDATE deals
                SET current_state = %s,
                    state_changed_at = %s,
                    state_changed_by = %s,
                    workspace_revision = %s
                WHERE id = %s AND tenant_id = %s AND workspace_revision = %s
                """,
                (to, now, actor_id, new_revision, deal_id, tenant_id, expected_revision),
            )
            if cur.rowcount == 0:
                raise DealRevisionMismatch(expected_revision, current_revision)
        else:
            cur.execute(
                """
                UPDATE deals
                SET current_state = %s,
                    state_changed_at = %s,
                    state_changed_by = %s,
                    workspace_revision = %s
                WHERE id = %s AND tenant_id = %s
                """,
                (to, now, actor_id, new_revision, deal_id, tenant_id),
            )
        cur.execute(
            """
            INSERT INTO deal_state_transitions (
                tenant_id, deal_id, from_state, to_state, reason,
                actor_id, correlation_id, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (tenant_id, deal_id, from_state, to, reason, actor_id, correlation_id, now),
        )
        record_deal_event(
            cur,
            tenant_id=tenant_id,
            deal_id=deal_id,
            actor_id=actor_id,
            action="state.transition",
            entity_type="deal",
            entity_id=deal_id,
            metadata={**(audit_metadata or {}), "from": from_state, "to": to, "reason": reason},
            correlation_id=correlation_id,
            created_at=now,
        )
    return {
        "deal_id": deal_id,
        "from_state": from_state,
        "current_state": to,
        "workspace_revision": new_revision,
        "allowed_transitions": state_machine.allowed_from(to),
        "state_changed_at": now.isoformat(),
    }


class DealDocumentEmpty(Exception):
    """Raised when the uploaded file has zero bytes."""


class DealDocumentTypeUnsupported(Exception):
    """Raised when the MIME type is not in the allowlist."""

    def __init__(self, mime_type: str) -> None:
        self.mime_type = mime_type
        super().__init__(f"mime type {mime_type} is not supported")


class DealDocumentTooLarge(Exception):
    """Raised when the file exceeds MAX_UPLOAD_BYTES."""

    def __init__(self, size: int) -> None:
        self.size = size
        super().__init__(f"file size {size} exceeds maximum")


class DealDocumentDuplicate(Exception):
    """Raised when a file with the same SHA256 hash already exists in the deal."""


ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
    "text/plain",
    "image/png",
    "image/jpeg",
}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
STORAGE_ROOT = Path(os.getenv("CREDITPAPER_DOCUMENT_ROOT", "var/tenant_documents"))


def _safe_filename(name: str) -> str:
    base = Path(name).name
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return sanitized or "upload.bin"


def _storage_path(
    *,
    tenant_id: str,
    deal_id: str,
    document_id: str,
    version_id: str,
    original_filename: str,
) -> Path:
    # Keep the original filename in the database, not the disk path: the deep
    # tenant/deal/version directories plus long client filenames can exceed
    # Windows MAX_PATH. Retain only known short extensions for format sniffing.
    suffix = Path(_safe_filename(original_filename)).suffix.lower()
    if suffix not in {".csv", ".docx", ".jpeg", ".jpg", ".pdf", ".png", ".txt", ".xlsx"}:
        suffix = ""
    return STORAGE_ROOT / str(tenant_id) / deal_id / document_id / f"{version_id}{suffix}"


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def upload_document(
    *,
    tenant_id: str,
    deal_id: str,
    actor_id: str,
    file_bytes: bytes,
    original_filename: str,
    mime_type: str,
    display_name: str | None,
) -> dict:
    result = upload_document_batch(
        tenant_id=str(tenant_id),
        deal_id=deal_id,
        actor_id=str(actor_id),
        uploads=[
            {
                "client_key": "single",
                "file_bytes": file_bytes,
                "original_filename": original_filename,
                "mime_type": mime_type,
                "display_name": display_name,
            }
        ],
    )[0]
    if result["status"] == "not_found":
        raise DealNotFound(deal_id)
    if result["status"] == "duplicate":
        raise DealDocumentDuplicate(
            "Duplicate file: same content already uploaded as "
            f"'{result.get('display_name')}' (version {result.get('version_number')}). "
            f"File hash: {result['content_sha256'][:16]}..."
        )
    if result["status"] != "ok":
        raise RuntimeError("Document upload routine returned an invalid result")
    return result["upload"]


def upload_document_batch(
    *,
    tenant_id: str,
    deal_id: str,
    actor_id: str,
    uploads: list[dict],
    source_batch_id: str | None = None,
) -> list[dict]:
    """Encrypt and atomically register up to 50 documents in one DB call.

    Each result retains its caller-provided ``client_key``. Business rejections
    are returned per item; connection/transaction uncertainty raises and keeps
    encrypted orphan files for a safe retry/repair pass.
    """
    if not 1 <= len(uploads) <= 50:
        raise ValueError("upload batch must contain 1 to 50 documents")

    from creditpaper.db import tenant_cursor
    from creditpaper.security.document_storage import write_document

    prepared: list[dict] = []
    for item in uploads:
        file_bytes = item["file_bytes"]
        mime_type = item["mime_type"]
        original_filename = item["original_filename"]
        if not file_bytes:
            raise DealDocumentEmpty("empty file")
        if mime_type not in ALLOWED_MIME_TYPES:
            raise DealDocumentTypeUnsupported(mime_type)
        if len(file_bytes) > MAX_UPLOAD_BYTES:
            raise DealDocumentTooLarge(len(file_bytes))

        safe_original = _safe_filename(original_filename)
        document_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        uploaded_at = datetime.now(UTC)
        path = _storage_path(
            tenant_id=tenant_id,
            deal_id=deal_id,
            document_id=document_id,
            version_id=version_id,
            original_filename=safe_original,
        )
        _ensure_dir(path)
        write_document(path, file_bytes, tenant_id=tenant_id, record_id=version_id)
        prepared.append(
            {
                "client_key": str(item["client_key"]),
                "document_id": document_id,
                "version_id": version_id,
                "display_name": (item.get("display_name") or "").strip() or safe_original,
                "original_filename": safe_original,
                "mime_type": mime_type,
                "size_bytes": len(file_bytes),
                "content_sha256": hashlib.sha256(file_bytes).hexdigest(),
                "storage_path": str(path),
                "correlation_id": str(uuid.uuid4()),
                "uploaded_at": uploaded_at.isoformat(),
                **({"batch_id": source_batch_id} if source_batch_id else {}),
            }
        )

    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.create_deal_document_upload_batch(%s::uuid,%s,%s::uuid,%s::jsonb)",
            (tenant_id, deal_id, actor_id, json.dumps(prepared)),
        )
        outcomes = cur.fetchone()[0]

    by_key = {str(outcome["client_key"]): outcome for outcome in outcomes}
    results: list[dict] = []
    for item in prepared:
        outcome = by_key.get(item["client_key"], {"status": "invalid"})
        status = outcome.get("status")
        if status != "ok":
            Path(item["storage_path"]).unlink(missing_ok=True)
            results.append({**outcome, "content_sha256": item["content_sha256"]})
            continue
        document = outcome["document"]
        results.append(
            {
                **outcome,
                "content_sha256": item["content_sha256"],
                "upload": {
                    "document": {
                        "id": str(document["id"]),
                        "display_name": document["display_name"],
                        "document_kind": document.get("document_kind"),
                        "created_at": document.get("created_at"),
                    },
                    "version": {
                        "id": item["version_id"],
                        "version_number": int(outcome["version_number"]),
                        "original_filename": item["original_filename"],
                        "mime_type": item["mime_type"],
                        "size_bytes": item["size_bytes"],
                        "content_sha256": item["content_sha256"],
                        "storage_path": item["storage_path"],
                        "uploaded_by": actor_id,
                        "uploaded_at": item["uploaded_at"],
                        "processing_status": "pending",
                    },
                    "extraction_status": "pending",
                    "job_id": str(outcome["job_id"]),
                },
            }
        )
    return results


def stream_version(
    *,
    tenant_id: str,
    deal_id: str,
    document_id: str,
    version_id: str,
) -> tuple[bytes, str, str]:
    with cursor() as cur:
        cur.execute(
            "SELECT storage_path, original_filename, mime_type "
            "FROM deal_document_versions "
            "WHERE id = %s AND document_id = %s AND deal_id = %s AND tenant_id = %s",
            (version_id, document_id, deal_id, tenant_id),
        )
        row = cur.fetchone()
        if not row:
            raise DealNotFound(f"document version {version_id}")

        storage_path, original_filename, mime_type = row
        path = Path(storage_path)
        if not path.exists():
            raise DealNotFound(f"file for version {version_id}")

        return path.read_bytes(), original_filename, mime_type


def _load_deal_model(deal_id: str, tenant_id: str) -> dict | None:
    """Load the current time-scoped model projection."""
    from creditpaper.ledger.service import load_model

    model = load_model(tenant_id, deal_id)
    return model.model_dump(mode="json") if model else None


def _get_deal_documents(deal_id: str, tenant_id: str) -> list[dict]:
    """Return all non-archived document versions for the deal."""
    with cursor() as cur:
        cur.execute(
            """
            SELECT dv.id, dv.storage_path, dv.original_filename, dv.document_id
            FROM deal_document_versions dv
            JOIN deal_documents dd ON dd.id = dv.document_id
            WHERE dd.deal_id = %s AND dd.tenant_id = %s AND dd.archived_at IS NULL
                AND dv.superseded_at IS NULL
            ORDER BY dv.version_number DESC
            """,
            (deal_id, tenant_id),
        )
        return [
            {"id": str(r[0]), "storage_path": r[1], "original_filename": r[2], "document_id": str(r[3])}
            for r in cur.fetchall()
        ]


def snapshot_model(deal_id: str, model_data: dict, tenant_id: str) -> dict:
    """Import a legacy snapshot through the ledger, preserving evidence and conflicts."""
    from creditpaper.ledger.service import commit_outputs, facts_in
    from creditpaper.model.apply import ExtractionOutput
    from creditpaper.model.deal import DealModel

    model = DealModel.model_validate(model_data)
    if model.deal_id != deal_id or model.tenant_id != str(tenant_id):
        raise ValueError("Snapshot scope does not match the deal")
    saved = commit_outputs(str(tenant_id), deal_id, [ExtractionOutput(facts_in(model), [])], legacy=True)
    return {"deal_id": deal_id, "version": saved.model_version, "model": saved.model_dump(mode="json")}


def _snapshot_model(deal_id: str, tenant_id: str, model_data: dict) -> None:
    """Persist the DealModel snapshot to the database (internal)."""
    snapshot_model(deal_id, model_data, tenant_id)


# Per-deal lock: prevents concurrent model writes from overlapping extraction jobs.
_extraction_locks: dict[str, threading.Lock] = {}
_extraction_locks_guard = threading.Lock()


def _get_deal_lock(deal_id: str) -> threading.Lock:
    """Get or create a per-deal extraction lock."""
    with _extraction_locks_guard:
        if deal_id not in _extraction_locks:
            _extraction_locks[deal_id] = threading.Lock()
        return _extraction_locks[deal_id]


@timed("domain_processing")
def run_single_document_extraction(
    deal_id: str, document_id: str, version_id: str, tenant_id: str, extractor: str | None = None
) -> dict:
    """Run one document through the extractor selected by the confirmed slot."""
    import logging

    from creditpaper.deals.utils.ingest import ingest
    from creditpaper.harness.route import EXTRACTOR_REGISTRY, classify
    from creditpaper.llm.tenant_config import provider_session
    from creditpaper.model import DealModel

    logger = logging.getLogger(__name__)
    tid = str(tenant_id)
    lock = _get_deal_lock(deal_id)
    if not lock.acquire(blocking=False):
        return {"status": "queued", "document_id": document_id}

    document_name: str | None = None
    try:
        # Update status to processing
        with cursor() as cur:
            cur.execute(
                "UPDATE deal_document_versions SET extraction_status = 'processing' WHERE id = %s",
                (version_id,),
            )

        # Get the document's storage path and filename
        with cursor() as cur:
            cur.execute(
                """
                SELECT v.storage_path, v.original_filename, d.document_kind, d.display_name
                FROM deal_document_versions v
                JOIN deal_documents d ON v.document_id = d.id
                WHERE v.id = %s
                """,
                (version_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Version {version_id} not found")

        storage_path, original_filename, document_kind, display_name = row
        document_name = display_name or original_filename
        file_path = Path(storage_path)

        # Classify the document to find its slot
        slot = extractor or classify(original_filename)
        if not slot or slot not in EXTRACTOR_REGISTRY:
            # No domain extractor for this document type — mark as invalid
            reason = f"No extractor available for document type '{slot or document_kind}'"
            with cursor() as cur:
                cur.execute(
                    "UPDATE deal_document_versions SET extraction_status = 'invalid', "
                    "extraction_error = %s WHERE id = %s",
                    (reason, version_id),
                )
            _record_extraction(tid, deal_id, document_id, document_name, error="no_extractor")
            return {"status": "invalid", "document_id": document_id, "slot": slot, "error": reason}

        # Load or create model
        model_data = _load_deal_model(deal_id, tid)
        if model_data is None:
            model_data = {
                "model_version": 1,
                "schema_version": DealModel.model_fields["schema_version"].default,
                "tenant_id": tid,
                "deal_id": deal_id,
            }
        model = DealModel(**model_data)

        # BYOK scope covers model calls and extraction child workers.
        with provider_session(tid):
            # Fetch page_texts only after opening tenant scope; DB row access is
            # still explicitly restricted to this version.
            with cursor() as cur:
                cur.execute(
                    "SELECT page_texts FROM deal_document_extractions "
                    "WHERE tenant_id = %s AND version_id = %s",
                    (tid, version_id),
                )
                pt_row = cur.fetchone()
                page_texts = pt_row[0] if pt_row and pt_row[0] else None

            # Run single-document extraction
            mappings = {slot: file_path}
            page_texts_map = {Path(file_path): page_texts} if page_texts else None
            report = ingest(
                model,
                mappings,
                page_texts_map=page_texts_map,
                document_ids={file_path: (document_id, version_id)},
            )

            # Snapshot the extracted outputs through the authoritative ledger.
            from creditpaper.ledger.service import commit_outputs

            model = commit_outputs(tid, deal_id, report.outputs)

        # Check if any facts were extracted
        total_facts = sum(report.facts_by_slot.values())
        if total_facts == 0 or report.failed_documents:
            # No facts extracted — mark as invalid
            reason = f"Document processed but no extractable facts found (extractor: {slot})"
            with cursor() as cur:
                cur.execute(
                    "UPDATE deal_document_versions SET extraction_status = 'invalid', "
                    "extraction_error = %s WHERE id = %s",
                    (reason, version_id),
                )
            _record_extraction(tid, deal_id, document_id, document_name, error="no_facts_found")
            return {
                "status": "invalid",
                "document_id": document_id,
                "slot": slot,
                "error": reason,
                "facts_by_slot": report.facts_by_slot,
                "applied": report.applied,
            }

        # Mark as ready
        with cursor() as cur:
            cur.execute(
                "UPDATE deal_document_versions SET extraction_status = 'ready' WHERE id = %s",
                (version_id,),
            )
        _record_extraction(tid, deal_id, document_id, document_name, facts_count=total_facts)

        return {
            "status": "ready",
            "document_id": document_id,
            "slot": slot,
            "facts_by_slot": report.facts_by_slot,
            "applied": report.applied,
        }

    except Exception as exc:
        logger.error("Domain extraction failed for document %s: %s", document_id, type(exc).__name__)
        with cursor() as cur:
            cur.execute(
                "UPDATE deal_document_versions SET extraction_status = 'failed' WHERE id = %s",
                (version_id,),
            )
        _record_extraction(tid, deal_id, document_id, document_name, error="extraction_error")
        return {"status": "failed", "document_id": document_id, "error": str(exc)}
    finally:
        lock.release()


def _record_extraction(
    tenant_id: str,
    deal_id: str,
    document_id: str,
    document_name: str | None,
    *,
    facts_count: int | None = None,
    error: str | None = None,
) -> None:
    """Best-effort Activity event for one document's extraction. Never raises into the job.
    `error` is a short code, never the exception text (which can quote document content)."""
    if error is None:
        action = "extraction.completed"
        metadata: dict = {
            "document_id": document_id,
            "document_name": document_name,
            "facts_count": facts_count,
        }
    else:
        action = "extraction.failed"
        metadata = {"document_id": document_id, "document_name": document_name, "error": error}
    record_deal_event_best_effort(
        tenant_id=tenant_id,
        deal_id=deal_id,
        actor_id=None,
        action=action,
        entity_type="deal_document",
        entity_id=document_id,
        metadata=metadata,
    )


def trigger_document_extraction(version_id: str, deal_id: str, tenant_id: str) -> None:
    """Trigger S3 domain extraction after S2 text extraction completes.

    Polls processing_status until S2 completes, then runs the domain
    extractor for this specific document.
    """
    import time

    tid = str(tenant_id)

    # Wait for S2 to complete (poll processing_status)
    max_wait = 300  # 5 minutes max
    poll_interval = 2  # seconds
    elapsed = 0

    while elapsed < max_wait:
        with cursor() as cur:
            cur.execute(
                "SELECT processing_status FROM deal_document_versions WHERE id = %s",
                (version_id,),
            )
            row = cur.fetchone()
            if row and row[0] in ("ready", "failed"):
                break
        time.sleep(poll_interval)
        elapsed += poll_interval

    # Check if S2 completed successfully
    with cursor() as cur:
        cur.execute(
            "SELECT processing_status FROM deal_document_versions WHERE id = %s",
            (version_id,),
        )
        row = cur.fetchone()
        if not row or row[0] != "ready":
            # S2 didn't complete — set extraction_status to failed and return
            with cursor() as cur2:
                cur2.execute(
                    "UPDATE deal_document_versions SET extraction_status = 'failed' WHERE id = %s",
                    (version_id,),
                )
            return

    # Get document_id for this version
    with cursor() as cur:
        cur.execute(
            "SELECT document_id FROM deal_document_versions WHERE id = %s",
            (version_id,),
        )
        row = cur.fetchone()
        if not row:
            return
        document_id = str(row[0])

    # Run domain extraction
    run_single_document_extraction(deal_id, document_id, version_id, tid)


@timed("deal_processing")
def _deal_model_or_new(deal_id: str, tid: str):
    """The deal's current model, or an empty one at the current schema."""
    from creditpaper.model import DealModel

    model_data = _load_deal_model(deal_id, tid)
    if model_data is None:
        model_data = {
            "model_version": 1,
            "schema_version": DealModel.model_fields["schema_version"].default,
            "tenant_id": tid,
            "deal_id": deal_id,
        }
    return DealModel(**model_data)


def _confirmed_mappings(tenant_id: str, deal_id: str, docs: list[dict]) -> dict[str, list[Path]]:
    """{routing key: [file, ...]} from the slots the analyst decided.

    The board speaks taxonomy keys (`signed_its`); the extractor registry speaks
    routing keys (`its`), so every key is converted explicitly — a string passed
    across that boundary does not error, it silently matches nothing.

    A document in no decided slot is not extracted. That is the point: the
    pipeline never advances on an unconfirmed mapping (D3).
    """
    from creditpaper.deals.services.slot_matcher import routing_keys_for
    from creditpaper.deals.store import slot_store

    decided = {"confirmed", "processing", "failed"}
    slots = {
        s["id"]: s["slot_key"] for s in slot_store.list_slots(tenant_id, deal_id) if s["status"] in decided
    }
    paths = {d["document_id"]: d["storage_path"] for d in docs}

    out: dict[str, list[Path]] = {}
    # Deduplicated by DOCUMENT, not by path: a multi slot legitimately holds
    # several files, and two of them are two documents even when a test or a
    # storage layout gives them the same path string.
    seen: dict[str, set[str]] = {}
    for f in slot_store.list_slot_files(tenant_id, deal_id):
        if f["role"] != "assigned":
            continue
        slot_key = slots.get(str(f["slot_id"]))
        storage_path = paths.get(str(f["document_id"]))
        if slot_key is None or storage_path is None:
            continue
        for routing_key in routing_keys_for(slot_key):
            if str(f["document_id"]) in seen.setdefault(routing_key, set()):
                continue
            seen[routing_key].add(str(f["document_id"]))
            # A multi slot holds several files, and `ingest` takes a list.
            out.setdefault(routing_key, []).append(Path(storage_path))
    return out


def _slot_of_placed(tenant_id: str, deal_id: str) -> dict[str, str]:
    """Each placed document's slot (D3: assigned to a decided slot), for the list passes.

    The corpus read needs no slot; the targeted list and field-set passes do: they
    read only the documents filed in the slots a list comes from."""
    from creditpaper.deals.store import slot_store

    decided = {"confirmed", "processing", "failed"}
    slot_key = {
        str(s["id"]): str(s["slot_key"])
        for s in slot_store.list_slots(tenant_id, deal_id)
        if s["status"] in decided
    }
    return {
        str(f["document_id"]): slot_key[str(f["slot_id"])]
        for f in slot_store.list_slot_files(tenant_id, deal_id)
        if f["role"] == "assigned" and str(f["slot_id"]) in slot_key
    }


def _confirmed_documents(tenant_id: str, deal_id: str, docs: list[dict]) -> list[dict]:
    """The documents the analyst has PLACED, whatever slot they went into.

    `_confirmed_mappings` answers a different question: which routing key does
    each document feed, so the right extractor can be chosen. That question
    silently drops every document whose slot has no registered extractor, which
    is most of the board — and for the corpus path, which needs no extractor,
    dropping them is the whole loss it exists to prevent.

    D3 is unchanged and still enforced here: a document in no decided slot is
    not read. The analyst's placement is what puts a document in scope; what it
    was placed INTO is what this deliberately ignores.
    """
    from creditpaper.deals.store import slot_store

    decided = {"confirmed", "processing", "failed"}
    placed = {s["id"] for s in slot_store.list_slots(tenant_id, deal_id) if s["status"] in decided}
    by_document = {d["document_id"]: d for d in docs}

    out: list[dict] = []
    seen: set[str] = set()
    for f in slot_store.list_slot_files(tenant_id, deal_id):
        if f["role"] != "assigned" or str(f["slot_id"]) not in placed:
            continue
        doc_id = str(f["document_id"])
        doc = by_document.get(doc_id)
        if doc is None or doc_id in seen:
            continue
        seen.add(doc_id)
        out.append(doc)
    # Sorted by document id so the corpus is planned in the same order every
    # run. Window content hashes are order-independent, but apply order is not.
    return sorted(out, key=lambda d: d["document_id"])


def _primary_blocks(tenant_id: str, deal_id: str, docs: list[dict]) -> dict[str, set[str]]:
    """{document_id: the model blocks its slot PRIMARILY FEEDS}.

    The triage signal for the semantic support check. A fact landing outside
    these blocks is not wrong - a valuation report legitimately states DA and
    presale facts, and capturing them is what general extraction is FOR - but
    it is where misassignment concentrates, because the deterministic legs
    cannot tell "the value is in this quote" from "this quote states it of THIS
    field".

    Measured 2026-09-23 on blind_01: 196 of 517 facts land outside, so this
    turns a per-fact model call on every fact into one on roughly a third.
    """
    from creditpaper.deals.services.slot_taxonomy import slot_type_from_row
    from creditpaper.deals.store import slot_store

    blocks_by_key: dict[str, set[str]] = {}
    for row in slot_store.slot_types():
        slot = slot_type_from_row(row)
        blocks_by_key[slot.key] = set(slot.blocks())

    slot_keys = {s["id"]: s["slot_key"] for s in slot_store.list_slots(tenant_id, deal_id)}
    known = {d["document_id"] for d in docs}

    out: dict[str, set[str]] = {}
    for f in slot_store.list_slot_files(tenant_id, deal_id):
        doc_id = str(f["document_id"])
        if f["role"] != "assigned" or doc_id not in known:
            continue
        key = slot_keys.get(str(f["slot_id"]))
        if key:
            out.setdefault(doc_id, set()).update(blocks_by_key.get(key, set()))
    return out


def _page_texts_for(
    tenant_id: str, docs: list[dict]
) -> tuple[dict[Path, list[dict]], dict[Path, tuple[str, str]]]:
    """Parsed page text and storage identity for each document.

    The page text comes from `deal_document_extractions`, which is what
    `processing/extractors` wrote — including OCR. Reading the PDFs again here
    instead would silently drop the ~10% of pages that have no text layer.
    Read by the document's current version, scoped to the tenant.
    """
    page_texts_map: dict[Path, list[dict]] = {}
    document_ids: dict[Path, tuple[str, str]] = {}
    with cursor() as cur:
        for doc in docs:
            cur.execute(
                "SELECT page_texts FROM deal_document_extractions WHERE tenant_id=%s AND version_id=%s",
                (tenant_id, doc["id"]),
            )
            row = cur.fetchone()
            if row and row[0]:
                path = Path(doc["storage_path"])
                page_texts_map[path] = row[0]
                document_ids[path] = (doc["document_id"], doc["id"])
    return page_texts_map, document_ids


def run_extraction_pipeline(deal_id: str, job_id: str, tenant_id: str) -> None:
    """Run extraction for documents whose slot placement is decided."""
    from creditpaper.deals.utils.ingest import ingest
    from creditpaper.harness.route import EXTRACTOR_REGISTRY
    from creditpaper.llm.tenant_config import provider_session

    tid = str(tenant_id)
    with provider_session(tid):
        model = _deal_model_or_new(deal_id, tid)
        docs = _get_deal_documents(deal_id, tid)
        if not docs:
            return

        # NOT route_deal(). That is the filename classifier, and re-routing a
        # document the analyst has already placed is D3 head-on. The confirmed
        # slot IS the mapping.
        mappings = {
            key: paths
            for key, paths in _confirmed_mappings(tid, deal_id, docs).items()
            if key in EXTRACTOR_REGISTRY
        }
        if not mappings:
            return
        page_texts_map, _ = _page_texts_for(tid, docs)
        report = ingest(
            model,
            mappings,
            page_texts_map=page_texts_map or None,
            document_ids={Path(d["storage_path"]): (d["document_id"], d["id"]) for d in docs},
        )
        from creditpaper.ledger.service import commit_outputs

        # Commit what was read, THEN hold: one failed document must not discard
        # every document that succeeded (the 147-document run, e55ceba).
        commit_outputs(tid, deal_id, report.outputs)
        if report.failed_documents:
            from creditpaper.ledger.types import Hold

            raise Hold("Extraction Hold: a document failed extraction; retry current evidence")


def run_corpus_extraction_pipeline(
    deal_id: str, job_id: str, tenant_id: str, *, passes: int | None = None
) -> IngestReport | None:
    """Read every placed document once, against the whole field reference.

    The sibling of `run_extraction_pipeline`. Same load, same commit, same
    holds; the difference is which engine produces the outputs, and that one
    reads only the documents whose slot has a registered extractor while this
    one reads them all.

    Returns the report, or None where there was nothing to read. It used to
    return nothing, which discarded `cost_usd` — the ingestion slice of
    cost-of-sale, computed on every run and then dropped on the floor. The job
    dispatcher ignores return values, so nothing else changes.
    """
    from creditpaper.deals.utils.ingest import ingest_corpus
    from creditpaper.llm.tenant_config import provider_session

    tid = str(tenant_id)
    with provider_session(tid):
        model = _deal_model_or_new(deal_id, tid)

        docs = _get_deal_documents(deal_id, tid)
        if not docs:
            return None
        placed = _confirmed_documents(tid, deal_id, docs)
        if not placed:
            return None

        page_texts_map, document_ids = _page_texts_for(tid, placed)
        if not page_texts_map:
            # Nothing has been PARSED yet. Reading zero pages and committing the
            # empty result would mark the deal extracted with no facts, which is
            # indistinguishable from documents that state nothing.
            return None

        report = ingest_corpus(
            model,
            page_texts_map,
            document_ids=document_ids,
            tenant_id=tid,
            deal_id=deal_id,
            primary_blocks=_primary_blocks(tid, deal_id, placed),
            slot_of=_slot_of_placed(tid, deal_id),
            **({"passes": passes} if passes else {}),
        )

        from creditpaper.ledger.service import commit_outputs

        commit_outputs(tid, deal_id, report.outputs)
        if report.failed_documents:
            from creditpaper.ledger.types import Hold

            raise Hold("Extraction Hold: a document failed extraction; retry current evidence")
        return report


def run_extraction_pipeline_safe(deal_id: str, job_id: str, tenant_id: str) -> dict:
    """Run extraction with error handling and status updates.

    Wraps run_extraction_pipeline so background tasks never crash silently.
    On failure, marks all document versions as failed and returns a status dict.
    """
    import logging

    logger = logging.getLogger(__name__)

    try:
        run_extraction_pipeline(deal_id, job_id, tenant_id)
        return {"status": "completed", "job_id": job_id}
    except Exception as exc:
        logger.error("Extraction failed for deal %s: %s", deal_id, type(exc).__name__)
        with cursor() as cur:
            cur.execute(
                """
                UPDATE deal_document_versions
                SET extraction_status = 'failed'
                WHERE document_id IN (
                    SELECT id FROM deal_documents
                    WHERE deal_id = %s AND tenant_id = %s AND archived_at IS NULL
                )
                """,
                (deal_id, tenant_id),
            )
        return {"status": "failed", "job_id": job_id, "error": str(exc)}


def trigger_extraction_after_upload(version_id: str, deal_id: str, tenant_id: str) -> None:
    """Trigger S3 extraction after S2 text extraction completes.

    Polls processing_status until S2 completes, then runs the full
    extraction pipeline for the deal.
    """
    import time

    tid = str(tenant_id)

    # Wait for S2 to complete (poll processing_status)
    max_wait = 300  # 5 minutes max
    poll_interval = 2  # seconds
    elapsed = 0

    while elapsed < max_wait:
        with cursor() as cur:
            cur.execute(
                "SELECT processing_status FROM deal_document_versions WHERE id = %s",
                (version_id,),
            )
            row = cur.fetchone()
            if row and row[0] in ("ready", "failed"):
                break
        time.sleep(poll_interval)
        elapsed += poll_interval

    # Check if S2 completed successfully
    with cursor() as cur:
        cur.execute(
            "SELECT processing_status FROM deal_document_versions WHERE id = %s",
            (version_id,),
        )
        row = cur.fetchone()
        if not row or row[0] not in ("ready",):
            # S2 didn't complete — set extraction_status to failed and return
            with cursor() as cur2:
                cur2.execute(
                    "UPDATE deal_document_versions SET extraction_status = 'failed' WHERE id = %s",
                    (version_id,),
                )
            return

    # Update extraction_status to processing
    with cursor() as cur:
        cur.execute(
            "UPDATE deal_document_versions SET extraction_status = 'processing' WHERE id = %s",
            (version_id,),
        )

    # Run the full extraction pipeline
    try:
        run_extraction_pipeline(deal_id, str(uuid.uuid4()), tid)
        with cursor() as cur:
            cur.execute(
                "UPDATE deal_document_versions SET extraction_status = 'ready' WHERE id = %s",
                (version_id,),
            )
    except Exception:
        with cursor() as cur:
            cur.execute(
                "UPDATE deal_document_versions SET extraction_status = 'failed' WHERE id = %s",
                (version_id,),
            )
        raise
