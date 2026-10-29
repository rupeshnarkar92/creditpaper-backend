"""Bounded encrypted ZIP intake, processed by the durable job worker."""

from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from creditpaper.db import tenant_cursor
from creditpaper.ledger import jobs
from creditpaper.security.document_storage import materialize_document, write_document_from_path

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
# Decrypted document bytes are held only until one bulk registration call.
# Bound that group independently of file count so 25 large PDFs cannot make a
# worker allocate more than a gigabyte.
MAX_REGISTRATION_BYTES = 64 * 1024 * 1024
MAX_FILES = 1000
MIME_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_IGNORED_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
_TERMINAL_ITEM_STATES = ("uploaded", "duplicate", "skipped", "failed")


def summarize_item_states(items: dict) -> dict[str, int | dict[str, int]]:
    """Account for every archive path exactly once, including no-row outcomes."""
    counts = {
        state: sum(1 for item in items.values() if item.get("status") == state)
        for state in _TERMINAL_ITEM_STATES
    }
    reasons: dict[str, int] = {}
    for item in items.values():
        state = item.get("status")
        if state not in {"duplicate", "skipped", "failed"}:
            continue
        reason = item.get("message") or ("Duplicate content" if state == "duplicate" else state)
        reasons[reason] = reasons.get(reason, 0) + 1
    accounted = sum(counts.values())
    return {**counts, "accounted": accounted, "unaccounted": len(items) - accounted, "reasons": reasons}


def _skip_reason(entry: zipfile.ZipInfo) -> str | None:
    path = PurePosixPath(entry.filename)
    if path.name.lower() in _IGNORED_NAMES or "__MACOSX" in path.parts:
        return "Operating-system metadata"
    if entry.file_size == 0:
        return "Empty document"
    if entry.file_size > MAX_DOCUMENT_BYTES:
        return "Document exceeds 50 MB"
    if path.suffix.lower() not in MIME_TYPES:
        return "Unsupported document type"
    return None


def validate_archive(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = archive.infolist()
    if len(entries) > MAX_FILES * 2:
        raise ValueError("Too many archive entries")
    files = []
    names = set()
    expanded = 0
    for entry in entries:
        name = entry.filename
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or ":" in name
            or "\x00" in name
            or len(name) > 500
            or stat.S_ISLNK(entry.external_attr >> 16)
            or entry.flag_bits & 1
        ):
            raise ValueError("Archive contains an unsafe or encrypted entry")
        if entry.is_dir():
            continue
        normalized = str(path)
        if normalized in names:
            raise ValueError("Archive contains duplicate paths")
        names.add(normalized)
        expanded += entry.file_size
        if expanded > MAX_EXPANDED_BYTES or entry.file_size > max(entry.compress_size, 1) * 1000:
            raise ValueError("Archive exceeds expansion limits")
        files.append(entry)
    if not files or len(files) > MAX_FILES:
        raise ValueError("Archive must contain 1–1000 documents")
    return files


def accept_path(tenant: str, deal: str, batch: str, actor: str, source: Path) -> str:
    from creditpaper.deals.services.workspace import STORAGE_ROOT

    if source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("ZIP exceeds 256 MB; split the folder into smaller batches")
    with zipfile.ZipFile(source) as archive:
        count = len(validate_archive(archive))
    # Reserve enough logical queue space for the per-document parse jobs before
    # writing a large encrypted archive. This turns overload into an immediate,
    # retryable response instead of an apparently stuck upload.
    jobs.require_queue_capacity(tenant, deal, count + 1)
    # Random attempt filename prevents a duplicate HTTP submission overwriting
    # an archive already being processed. Store the authoritative path in DB.
    path = STORAGE_ROOT / tenant / "batches" / f"{uuid4()}.zip.enc"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_document_from_path(path, source, tenant_id=tenant, record_id=batch)
    # On uncertain DB commit retain encrypted bytes: deleting them could break
    # a job that committed just before a connection failure.
    with tenant_cursor(tenant) as cur:
        cur.execute(
            "SELECT public.accept_document_archive(%s::uuid,%s,%s::uuid,%s::uuid,%s,%s)",
            (tenant, deal, batch, actor, str(path), count),
        )
        job_id = str(cur.fetchone()[0])
    jobs.dispatch(tenant, job_id)
    return job_id


def accept(tenant: str, deal: str, batch: str, actor: str, data: bytes) -> str:
    """Compatibility adapter for callers that already own a bounded byte buffer."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    try:
        return accept_path(tenant, deal, batch, actor, temporary)
    finally:
        temporary.unlink(missing_ok=True)


def load(tenant: str, deal: str, batch: str) -> dict:
    with tenant_cursor(tenant) as cur:
        cur.execute("SELECT public.document_archive(%s::uuid,%s,%s::uuid)", (tenant, deal, batch))
        result = cur.fetchone()[0]
    if not result or not result["path"]:
        raise ValueError("Archive batch not found")
    return result


def checkpoint(tenant: str, deal: str, batch: str, items: dict, sealed: bool = False):
    with tenant_cursor(tenant) as cur:
        jobs.fence(cur)
        cur.execute(
            "SELECT public.document_archive_checkpoint(%s::uuid,%s,%s::uuid,%s::jsonb,%s)",
            (tenant, deal, batch, json.dumps(items), sealed),
        )


def run_import(tenant: str, deal: str, payload: dict) -> dict:
    from creditpaper.deals.services.workspace import (
        DealDocumentEmpty,
        DealDocumentTooLarge,
        DealDocumentTypeUnsupported,
        upload_document_batch,
    )

    batch = payload["batch_id"]
    saved = load(tenant, deal, batch)
    items = saved["items"]
    with (
        materialize_document(
            Path(saved["path"]), tenant_id=tenant, record_id=batch, suffix=".zip"
        ) as archive_path,
        zipfile.ZipFile(archive_path) as archive,
    ):
        entries = validate_archive(archive)
        checkpoint_every = 10
        since_checkpoint = 0
        pending: list[dict] = []
        pending_bytes = 0

        def persist(*, force: bool = False) -> None:
            nonlocal since_checkpoint
            if force or since_checkpoint >= checkpoint_every:
                checkpoint(tenant, deal, batch, items)
                since_checkpoint = 0

        def register_pending() -> None:
            nonlocal pending_bytes, since_checkpoint
            if not pending:
                return
            outcomes = upload_document_batch(
                tenant_id=tenant,
                deal_id=deal,
                actor_id=payload["actor_id"],
                uploads=pending,
                source_batch_id=batch,
            )
            for outcome in outcomes:
                name = outcome["client_key"]
                if outcome["status"] == "ok":
                    upload = outcome["upload"]
                    items[name] = {
                        "status": "uploaded",
                        "version_id": upload["version"]["id"],
                    }
                    jobs.dispatch(tenant, upload["job_id"])
                elif outcome["status"] == "resumed":
                    # The upload transaction committed before a worker crash,
                    # but its archive checkpoint did not. The stored batch
                    # fence proves this is the same item, not a new duplicate.
                    items[name] = {
                        "status": "uploaded",
                        "version_id": outcome["version_id"],
                    }
                    jobs.dispatch(tenant, outcome["job_id"])
                elif outcome["status"] == "duplicate":
                    items[name] = {"status": "duplicate"}
                else:
                    items[name] = {
                        "status": "failed",
                        "message": "Document registration failed",
                    }
                since_checkpoint += 1
            pending.clear()
            pending_bytes = 0
            persist(force=True)

        for entry in entries:
            name = entry.filename
            if items.get(name, {}).get("status") in {"uploaded", "duplicate"}:
                continue
            skip_reason = _skip_reason(entry)
            if skip_reason is not None:
                # Do not read/decrypt unusable entries. They are recorded as
                # skipped and never block usable documents in the batch.
                items[name] = {
                    "status": "skipped",
                    "message": skip_reason,
                }
                since_checkpoint += 1
                persist()
                continue
            mime_type = MIME_TYPES[PurePosixPath(name).suffix.lower()]
            try:
                if pending and pending_bytes + entry.file_size > MAX_REGISTRATION_BYTES:
                    register_pending()
                with archive.open(entry) as source:
                    content = source.read(MAX_DOCUMENT_BYTES + 1)
                pending.append(
                    {
                        "client_key": name,
                        "file_bytes": content,
                        "original_filename": PurePosixPath(name).name,
                        "display_name": name,
                        "mime_type": mime_type,
                    }
                )
                pending_bytes += len(content)
                if len(pending) >= 25:
                    register_pending()
            except (DealDocumentEmpty, DealDocumentTooLarge, DealDocumentTypeUnsupported):
                items[name] = {"status": "failed", "message": "Empty, oversized, or unsupported document"}
                since_checkpoint += 1
            except (zipfile.BadZipFile, NotImplementedError):
                items[name] = {"status": "failed", "message": "Document could not be read from ZIP"}
                since_checkpoint += 1
            persist()
        register_pending()
        # Always persist the tail, including batches smaller than the
        # checkpoint interval and the final unsupported/failed entries.
        if since_checkpoint:
            persist(force=True)
    # Matching is another durable job: claiming waits for parsing to settle.
    job_id = jobs.enqueue(tenant, deal, "archive_match", batch, payload)
    jobs.dispatch(tenant, job_id)
    counts = summarize_item_states(items)
    return {
        "status": "complete",
        "processed": len(items),
        "total": len(entries),
        **counts,
    }


def run_match(tenant: str, deal: str, payload: dict) -> dict:
    from creditpaper.deals.services.slot_board import propose

    propose(tenant, deal, payload["actor_id"], return_board=False)
    saved = load(tenant, deal, payload["batch_id"])
    checkpoint(tenant, deal, payload["batch_id"], saved["items"], sealed=True)
    return {"status": "complete"}
