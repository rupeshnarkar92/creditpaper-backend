"""Durable queue with bounded retries, expiring leases and fenced writes."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from creditpaper.db import tenant_cursor
from creditpaper.ledger.service import canonical, lock_deal
from creditpaper.ledger.types import Conflict, NotFound
from creditpaper.obs.ledger import CostLedger

log = logging.getLogger(__name__)

_LEASE: ContextVar[tuple[str, str, str] | None] = ContextVar("processing_lease", default=None)
_STAGE_TIMINGS: ContextVar[dict[str, int] | None] = ContextVar("processing_stage_timings", default=None)
_ACTIVE_LEASES: dict[str, tuple[str, str]] = {}
_ACTIVE_LEASES_LOCK = threading.Lock()
_HEARTBEAT_STARTED = False
_HEARTBEAT_INTERVAL_SECONDS = 20
_CLAIM_BATCH = min(16, max(1, int(os.getenv("PROCESSING_CLAIM_BATCH", "8"))))
_PREFETCH: dict[tuple[str, tuple[str, ...] | None], deque[dict]] = {}
_PREFETCH_EMPTY_UNTIL: dict[tuple[str, tuple[str, ...] | None], float] = {}
_PREFETCH_LOCK = threading.Lock()
MAX_ATTEMPTS = 3
_RETRY_DELAYS = (2, 4)
RETRY_BASE_SECONDS = _RETRY_DELAYS[0]
RETRY_MAX_SECONDS = _RETRY_DELAYS[-1]


class QueueOverloaded(RuntimeError):
    """A bounded tenant or deal queue cannot safely accept more work yet."""


def require_queue_capacity(tenant_id: str, deal_id: str, requested: int = 1) -> dict:
    """Fail quickly instead of accepting work that cannot meet the service SLO."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.processing_queue_admission(%s::uuid,%s,%s)",
            (tenant_id, deal_id, requested),
        )
        capacity = cur.fetchone()[0]
    if not capacity["accepted"]:
        raise QueueOverloaded("Processing queue is at capacity; retry shortly")
    return capacity


def current_lease() -> tuple[str, str, str] | None:
    """Return the active tenant/job/fencing token for persistence adapters."""
    return _LEASE.get()


def _retry_delay_seconds(attempt: int) -> int:
    """Exponential delay after a failed attempt; zero means the retry budget is spent."""
    if attempt < 1 or attempt >= MAX_ATTEMPTS:
        return 0
    return _RETRY_DELAYS[attempt - 1]


def _finish_job(tenant_id: str, job: dict, outcome: str, result: dict, error_type: str | None) -> str | None:
    """Atomically persist the final result, schedule a retry, or dead-letter a terminal failure."""
    terminal = result.get("retryable") is False or error_type in {"Hold", "BadInput", "NotFound"}
    retry_delay = _retry_delay_seconds(job["attempts"]) if outcome == "failed" and not terminal else 0
    from creditpaper.processing.job_finish_batch import finish_job

    return finish_job(
        tenant_id,
        {
            "id": job["id"],
            "lease": job["lease"],
            "outcome": outcome,
            "result": json.loads(canonical(result)),
            "error_type": error_type,
            "retry_delay": retry_delay,
            "stage_timings": _STAGE_TIMINGS.get() or {},
        },
    )


# Kinds that need the deal to themselves. Everything that writes the DEAL —
# the ledger, the model, the paper — must not run beside another writer, which
# is what the `busy` clause in `claim` enforces.
#
# `parse` is not one of them, and treating it as one made a folder drop serial.
# It reads one stored file and writes `deal_document_extractions` plus that
# version's own status row; it never touches the ledger or the deal model. With
# an analyst dropping a 144-document folder — which is the production case, not
# an edge one — 144 parse jobs queued behind each other at ~1.2s apiece, and
# that was the whole 180s before a single fact was extracted.
#
# So a parse job neither blocks nor is blocked. Two parse jobs for one deal are
# independent; a parse job and a generation job are independent too, because
# nothing the parse writes is state the generation reads mid-run.
EXCLUSIVE_KINDS: frozenset[str] = frozenset(
    {
        "generation",
        "document",
        "extraction",
        "corpus_extraction",
        "ingest_window",
        "ingest_speculate",
        "ingest_propose",
        "archive_match",
    }
)

# Worker lanes (spec §12). Generation holds a deal for minutes and spends the
# most model capacity, so it runs in its own lane with its own pool; the
# ingestion lane is everything else. "all" keeps the single-pool behaviour.
GENERATION_KINDS: frozenset[str] = frozenset({"generation"})
OCR_KINDS: frozenset[str] = frozenset({"parse_ocr"})
FAST_INGESTION_KINDS: frozenset[str] = frozenset(
    {
        "archive_import",
        "archive_match",
        "document",
        "extraction",
        "corpus_extraction",
        "parse",
        "ingest_window",
        "ingest_speculate",
        "ingest_propose",
    }
)
LANES: dict[str, frozenset[str] | None] = {
    "all": None,
    "generation": GENERATION_KINDS,
    "ingestion": FAST_INGESTION_KINDS | OCR_KINDS,
    "fast": FAST_INGESTION_KINDS,
    "ocr": OCR_KINDS,
}


def enqueue(tenant_id: str, deal_id: str, kind: str, resource_id: str, payload: dict) -> str:
    require_queue_capacity(tenant_id, deal_id)
    with tenant_cursor(tenant_id) as cur:
        lock_deal(cur, tenant_id, deal_id)
        if kind == "generation":
            cur.execute("SELECT public.deal_ingestion_status(%s::uuid,%s)", (tenant_id, deal_id))
            if cur.fetchone()[0]["locked"]:
                raise Conflict("Documents are still uploading, processing, or need attention")
        cur.execute(
            """INSERT INTO processing_jobs(tenant_id,deal_id,kind,resource_id,payload)
            VALUES(%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING id""",
            (tenant_id, deal_id, kind, resource_id, canonical(payload)),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])
        cur.execute(
            """SELECT id FROM processing_jobs WHERE tenant_id=%s AND deal_id=%s AND kind=%s
            AND resource_id=%s AND status IN ('pending','running')""",
            (tenant_id, deal_id, kind, resource_id),
        )
        return str(cur.fetchone()[0])


def status(tenant_id: str, deal_id: str, job_id: str) -> dict:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT status,result,payload,attempts,available_at,last_error_type FROM processing_jobs "
            "WHERE tenant_id=%s AND deal_id=%s AND id=%s",
            (tenant_id, deal_id, job_id),
        )
        row = cur.fetchone()
        if not row:
            raise NotFound("Processing job not found")
        return {
            **row[1],
            "status": row[0],
            "deal_id": deal_id,
            "tenant_id": tenant_id,
            "job_id": job_id,
            "attempts": row[3],
            "next_attempt_at": row[4].isoformat() if row[0] == "pending" and row[3] else None,
            "last_error_type": row[5],
            "dead_lettered": row[0] == "failed" and row[3] >= MAX_ATTEMPTS,
            "sections": row[1].get("sections", {str(s): "pending" for s in row[2].get("sections", [])}),
        }


def claim(tenant_id: str, job_id: str | None = None, *, kinds: frozenset[str] | None = None) -> dict | None:
    exclusive = sorted(EXCLUSIVE_KINDS)
    lane = sorted(kinds) if kinds is not None else None
    with tenant_cursor(tenant_id) as cur:
        # Claims are short transactions; serialize per tenant so one deal never
        # has competing document/generation workers across API processes.
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("queue:" + tenant_id,))
        cur.execute("SELECT sweep_processing_job_expired_leases(%s::uuid)", (tenant_id,))
        cur.execute(
            """SELECT j.id,j.deal_id,j.kind,j.payload,j.attempts+1,j.created_at FROM processing_jobs j
            WHERE j.tenant_id=%s AND (%s::uuid IS NULL OR j.id=%s)
              AND j.status='pending' AND j.available_at<=clock_timestamp()
              AND j.attempts<3
              AND (j.kind<>'archive_match' OR NOT EXISTS(
                SELECT 1 FROM processing_jobs other WHERE other.tenant_id=j.tenant_id
                  AND other.deal_id=j.deal_id AND other.kind IN ('parse','parse_ocr','archive_import')
                  AND other.status IN ('pending','running')
                  AND ((other.kind='archive_import' AND other.resource_id=j.resource_id)
                    OR (other.kind IN ('parse','parse_ocr')
                      AND other.payload->>'batch_id'=j.resource_id))))
              AND (j.kind<>'ingest_speculate' OR NOT EXISTS(
                SELECT 1 FROM processing_jobs matcher
                WHERE matcher.tenant_id=j.tenant_id AND matcher.deal_id=j.deal_id
                  AND matcher.kind='archive_match'
                  AND matcher.status IN ('pending','running')))
              -- A candidate that needs the deal to itself waits for any other
              -- EXCLUSIVE job on that deal. A parse job waits for nothing, and
              -- nothing waits for a parse job: see EXCLUSIVE_KINDS.
              AND (NOT (j.kind = ANY(%s)) OR NOT EXISTS(
                    SELECT 1 FROM processing_jobs busy
                    WHERE busy.tenant_id=j.tenant_id AND busy.deal_id=j.deal_id
                      AND busy.id<>j.id AND busy.status='running'
                      AND busy.lease_until>clock_timestamp()
                      AND busy.kind = ANY(%s)))
              AND (%s::text[] IS NULL OR j.kind = ANY(%s::text[]))
              -- At most one generation per tenant runs at a time.
              AND (j.kind <> 'generation' OR NOT EXISTS(
                    SELECT 1 FROM processing_jobs g
                    WHERE g.tenant_id=j.tenant_id AND g.kind='generation' AND g.id<>j.id
                      AND g.status='running' AND g.lease_until>clock_timestamp()))
            ORDER BY
              (SELECT count(*) FROM processing_jobs active
               WHERE active.tenant_id=j.tenant_id AND active.deal_id=j.deal_id
                 AND active.status='running' AND active.lease_until>clock_timestamp()),
              CASE j.kind WHEN 'archive_import' THEN 0 WHEN 'parse' THEN 10
                          WHEN 'parse_ocr' THEN 20 ELSE 30 END,
              j.created_at
            FOR UPDATE SKIP LOCKED LIMIT 1""",
            (tenant_id, job_id, job_id, exclusive, exclusive, lane, lane),
        )
        row = cur.fetchone()
        if not row:
            return None
        lease = str(uuid4())
        cur.execute(
            """UPDATE processing_jobs SET status='running',attempts=attempts+1,lease_token=%s,
            lease_until=clock_timestamp()+interval '90 seconds',updated_at=clock_timestamp()
            WHERE tenant_id=%s AND id=%s""",
            (lease, tenant_id, row[0]),
        )
        return {
            "id": str(row[0]),
            "deal_id": row[1],
            "kind": row[2],
            "payload": row[3],
            "attempts": row[4],
            "lease": lease,
            "queue_wait_ms": max(0, round((time.time() - row[5].timestamp()) * 1000)),
        }


def _claim_prefetched(tenant_id: str, kinds: frozenset[str] | None) -> dict | None:
    """Share one database batch claim among concurrent worker threads."""
    lane = tuple(sorted(kinds)) if kinds is not None else None
    key = (tenant_id, lane)
    with _PREFETCH_LOCK:
        queue = _PREFETCH.setdefault(key, deque())
        if queue:
            return queue.popleft()
        if _PREFETCH_EMPTY_UNTIL.get(key, 0) > time.monotonic():
            return None
        from creditpaper.processing.store import PARSER_VERSION

        with tenant_cursor(tenant_id) as cur:
            cur.execute(
                "SELECT public.claim_processing_jobs_prepared(%s::uuid,%s,%s::text[],%s)",
                (
                    tenant_id,
                    _CLAIM_BATCH,
                    list(lane) if lane is not None else None,
                    PARSER_VERSION,
                ),
            )
            claimed = cur.fetchone()[0]
        if not claimed:
            _PREFETCH_EMPTY_UNTIL[key] = time.monotonic() + 1.0
            return None
        _PREFETCH_EMPTY_UNTIL.pop(key, None)
        # A batch lease exists before every item has reached run_one(). Keep
        # the whole batch fenced and alive immediately; otherwise a long job
        # occupying another compute slot can let a prefetched lease expire.
        _ensure_heartbeat_thread()
        with _ACTIVE_LEASES_LOCK:
            for prefetched in claimed:
                _ACTIVE_LEASES[prefetched["id"]] = (tenant_id, prefetched["lease"])
        queue.extend(claimed)
        return queue.popleft()


def fence(cur) -> None:
    lease = _LEASE.get()
    if lease:
        cur.execute(
            """SELECT 1 FROM processing_jobs WHERE tenant_id=%s AND id=%s AND lease_token=%s
            AND status='running' AND lease_until>clock_timestamp() FOR UPDATE""",
            lease,
        )
        if not cur.fetchone():
            raise Conflict("Processing lease expired; stale worker cannot write")


def progress(result: dict) -> None:
    lease = _LEASE.get()
    if not lease:
        return
    with tenant_cursor(lease[0]) as cur:
        fence(cur)
        cur.execute(
            "UPDATE processing_jobs SET result=%s::jsonb,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND id=%s AND lease_token=%s",
            (canonical(result), *lease),
        )


def stage_checkpoint(
    stage: str,
    state: str,
    *,
    elapsed_ms: int | None = None,
    metadata: dict | None = None,
) -> bool:
    """Persist a stage checkpoint for the active fenced job lease.

    Callers do not need to know job ids or lease tokens. Outside a worker job
    (for example a direct unit test or repair command) this is a harmless no-op.
    Metadata must be operational counters only; document content never belongs
    in the queue telemetry row.
    """
    lease = _LEASE.get()
    if not lease:
        return False
    with tenant_cursor(lease[0]) as cur:
        cur.execute(
            """SELECT public.checkpoint_processing_job_stage(
                %s::uuid,%s::uuid,%s::uuid,%s,%s,%s,%s::jsonb)""",
            (
                *lease,
                stage,
                state,
                elapsed_ms,
                canonical(metadata or {}),
            ),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        raise Conflict("Processing lease expired; stage checkpoint was rejected")
    return True


@contextmanager
def processing_stage(stage: str, *, metadata: dict | None = None):
    """Measure one stage, checkpointing only boundaries useful after a crash."""
    started = time.perf_counter()
    details = dict(metadata or {})
    try:
        yield details
    except Exception:
        timings = _STAGE_TIMINGS.get()
        if timings is not None:
            timings[stage] = round((time.perf_counter() - started) * 1000)
        raise
    else:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        timings = _STAGE_TIMINGS.get()
        if timings is not None:
            timings[stage] = elapsed_ms


def _renew_active_leases() -> None:
    """Renew every active lease with one transaction per tenant."""
    with _ACTIVE_LEASES_LOCK:
        snapshot = dict(_ACTIVE_LEASES)
    by_tenant: dict[str, list[dict[str, str]]] = {}
    for job_id, (tenant_id, lease) in snapshot.items():
        by_tenant.setdefault(tenant_id, []).append({"id": job_id, "lease": lease})
    for tenant_id, leases in by_tenant.items():
        try:
            with tenant_cursor(tenant_id) as cur:
                cur.execute(
                    "SELECT public.refresh_worker_leases(%s::uuid,%s::jsonb)",
                    (tenant_id, json.dumps(leases)),
                )
        except Exception:
            # The completion fence remains authoritative if a renewal is lost.
            continue


def _heartbeat_loop() -> None:
    while True:
        time.sleep(_HEARTBEAT_INTERVAL_SECONDS)
        _renew_active_leases()


def _ensure_heartbeat_thread() -> None:
    global _HEARTBEAT_STARTED
    with _ACTIVE_LEASES_LOCK:
        if _HEARTBEAT_STARTED:
            return
        _HEARTBEAT_STARTED = True
    threading.Thread(
        target=_heartbeat_loop,
        daemon=True,
        name="processing-heartbeat",
    ).start()


@contextmanager
def _heartbeat(tenant_id: str, job: dict, *, renew: bool = True):
    if renew:
        _ensure_heartbeat_thread()
        with _ACTIVE_LEASES_LOCK:
            _ACTIVE_LEASES[job["id"]] = (tenant_id, job["lease"])
    token = _LEASE.set((tenant_id, job["id"], job["lease"]))
    timings_token = _STAGE_TIMINGS.set({"queue_wait": int(job.get("queue_wait_ms", 0))})
    try:
        yield
    finally:
        _STAGE_TIMINGS.reset(timings_token)
        _LEASE.reset(token)
        if renew:
            with _ACTIVE_LEASES_LOCK:
                current = _ACTIVE_LEASES.get(job["id"])
                if current == (tenant_id, job["lease"]):
                    del _ACTIVE_LEASES[job["id"]]


def _llm_usage(ledger: CostLedger) -> dict[str, int | float | bool]:
    """The safe accounting summary persisted with durable ingestion results."""
    return {
        "calls": ledger.calls,
        "cost_usd": round(ledger.total_usd, 6),
        "unpriced": ledger.has_unpriced,
        "failed_calls": ledger.failed_calls,
    }


def _claim_broker_job(tenant_id: str, job_id: str, task_id: str) -> dict | None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.claim_processing_job_for_broker(%s::uuid,%s::uuid,%s,%s)",
            (tenant_id, job_id, task_id, 90),
        )
        return cur.fetchone()[0]


def status_for_job(tenant_id: str, job_id: str) -> dict:
    """Read minimal durable state used by a broker task after execution."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.processing_job_delivery_status(%s::uuid,%s::uuid)",
            (tenant_id, job_id),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else {"status": "complete", "retry_in_seconds": 0}


def run_one(
    tenant_id: str,
    job_id: str | None = None,
    *,
    kinds: frozenset[str] | None = None,
    renew_lease: bool = True,
    broker_task_id: str | None = None,
) -> bool:
    if broker_task_id is not None:
        if job_id is None:
            raise ValueError("broker execution requires a job id")
        job = _claim_broker_job(tenant_id, job_id, broker_task_id)
    else:
        job = claim(tenant_id, job_id, kinds=kinds) if job_id else _claim_prefetched(tenant_id, kinds)
    if not job:
        return False
    job_started = time.perf_counter()
    log.info(
        "processing_job_started tenant_id=%s deal_id=%s job_id=%s kind=%s attempt=%d queue_wait_ms=%d",
        tenant_id,
        job["deal_id"],
        job["id"],
        job["kind"],
        job["attempts"],
        job["queue_wait_ms"],
    )
    outcome = "complete"
    error_type: str | None = None
    result: dict = {}
    usage_ledger: CostLedger | None = None
    with _heartbeat(tenant_id, job, renew=renew_lease):
        try:
            if job["kind"] == "generation":
                from creditpaper.deals.services.generation import _run_generation
                from creditpaper.llm.ollama import ollama_stage_timeout

                with processing_stage("model_generation"), ollama_stage_timeout("generation"):
                    result = _run_generation(
                        job["id"],
                        job["deal_id"],
                        tenant_id,
                        job["payload"]["sections"],
                        actor_id=job["payload"].get("actor_id"),
                    )
                outcome = result.get("status", "complete")
            elif job["kind"] in {"archive_import", "archive_match"}:
                from creditpaper.deals.services.archive_intake import run_import, run_match

                handler = run_import if job["kind"] == "archive_import" else run_match
                result = handler(tenant_id, job["deal_id"], job["payload"])
            elif job["kind"] == "parse":
                from creditpaper.processing.pipeline import OCRRequired, process_version

                try:
                    ready = process_version(
                        job["payload"]["version_id"],
                        tenant_id=tenant_id,
                        deal_id=job["deal_id"],
                        allow_ocr=False,
                        prepared=job.get("processing_input"),
                    )
                    result = {"status": "complete" if ready else "failed"}
                    if not ready:
                        result.update(
                            retryable=False,
                            error_type="DocumentProcessingFailed",
                            error="Document could not be parsed",
                        )
                        outcome = "failed"
                except OCRRequired as deferred:
                    from creditpaper.processing.store import defer_to_ocr

                    ocr_job = defer_to_ocr(
                        tenant_id,
                        job["deal_id"],
                        job["payload"]["version_id"],
                        job["payload"],
                    )
                    dispatch(tenant_id, ocr_job)
                    result = {"status": "complete", "deferred_to_ocr": True, "pages": deferred.pages}
            elif job["kind"] == "parse_ocr":
                from creditpaper.processing.pipeline import process_version

                ready = process_version(
                    job["payload"]["version_id"],
                    tenant_id=tenant_id,
                    deal_id=job["deal_id"],
                    allow_ocr=True,
                    prepared=job.get("processing_input"),
                )
                result = {"status": "complete" if ready else "failed"}
                if not ready:
                    # OCR exhaustively attempted the selected pages. Retrying
                    # the identical unreadable bytes only burns the lane and
                    # delays usable documents; a user can explicitly retry
                    # after replacing/fixing the source file.
                    result.update(
                        retryable=False,
                        error_type="UnreadableDocument",
                        error="Document remained unreadable after OCR",
                    )
                    outcome = "failed"
            elif job["kind"] == "document":
                from creditpaper.deals.services.workspace import run_single_document_extraction
                from creditpaper.processing.pipeline import process_version

                parsed = process_version(
                    job["payload"]["version_id"],
                    tenant_id=tenant_id,
                    deal_id=job["deal_id"],
                    prepared=job.get("processing_input"),
                )
                if not parsed:
                    result = {"status": "failed", "error": "Document parsing failed"}
                    outcome = "failed"
                else:
                    result = run_single_document_extraction(
                        job["deal_id"],
                        job["payload"]["document_id"],
                        job["payload"]["version_id"],
                        tenant_id,
                        extractor=job["payload"].get("extractor"),
                    )
                    if result.get("status") != "ready":
                        outcome = "failed"
            elif job["kind"] == "corpus_extraction":
                from creditpaper.deals.services.workspace import run_corpus_extraction_pipeline

                # General extraction: every placed document read once against
                # the whole field reference, rather than each document routed
                # to the extractor registered for its slot. A separate KIND and
                # not a flag on "extraction", so which engine ran a deal is a
                # fact in the job log rather than something to infer.
                run_corpus_extraction_pipeline(
                    job["deal_id"],
                    job["id"],
                    tenant_id,
                    passes=int(job["payload"].get("passes") or 0) or None,
                )
                result = {"status": "complete"}
            elif job["kind"] in {"ingest_window", "ingest_speculate", "ingest_propose"}:
                # These jobs call the model directly (or fan out to child
                # extraction workers), so they need the same tenant BYOK and
                # usage-attribution scope as the workspace extraction paths.
                from creditpaper.llm.ollama import ollama_stage_timeout
                from creditpaper.llm.tenant_config import provider_session
                from creditpaper.obs.ledger import cost_session

                with provider_session(tenant_id), cost_session(deal_id=job["deal_id"]) as ledger:
                    usage_ledger = ledger
                    with processing_stage(f"model_{job['kind']}"), ollama_stage_timeout("facts"):
                        if job["kind"] == "ingest_window":
                            from creditpaper.ingest.commit import run_one_window

                            result = run_one_window(tenant_id, job["deal_id"], job["payload"])
                            if result.get("status") != "complete":
                                outcome = "failed"
                        elif job["kind"] == "ingest_speculate":
                            from creditpaper.ingest.speculate import run_speculation

                            result = run_speculation(tenant_id, job["deal_id"], job["payload"])
                        else:
                            from creditpaper.ingest.jobs import run_propose

                            result = run_propose(tenant_id, job["deal_id"], job["payload"])
                            # The rows are already recorded against the deal; the
                            # job log keeps counts, never document text or filenames.
                            result.pop("rows", None)
                    # Persist only accounting totals, never prompts, facts or provider errors.
                    result["llm_usage"] = _llm_usage(ledger)
            else:
                from creditpaper.deals.services.workspace import run_extraction_pipeline

                run_extraction_pipeline(job["deal_id"], job["id"], tenant_id)
                result = {"status": "complete"}
            if outcome == "failed":
                error_type = result.get("error_type") or "ProcessingFailure"
                result = {
                    **result,
                    "status": "failed",
                    "error": result.get("error") or "Processing failed",
                    "error_type": error_type,
                }
                if usage_ledger is not None:
                    result["llm_usage"] = _llm_usage(usage_ledger)
        except (OperationalError, PoolTimeout):
            # Broker workers retry infrastructure pressure themselves. Do not
            # spend the document's three-attempt business retry budget merely
            # because a database checkout was temporarily unavailable.
            raise
        except Exception as exc:
            from creditpaper.ledger.types import BadInput, Hold

            outcome = "failed"
            error_type = type(exc).__name__
            # No prompts, document paths, extracted facts or provider errors enter the job log.
            error_result = {"error": "Processing failed", "error_type": type(exc).__name__}
            if isinstance(exc, (BadInput, Hold, NotFound)):
                error_result["retryable"] = False
            if usage_ledger is not None:
                error_result["llm_usage"] = _llm_usage(usage_ledger)
            result = error_result
        if outcome == "failed" and error_type is None:
            error_type = "ProcessingFailure"
        finished_status = _finish_job(tenant_id, job, outcome, result, error_type)
        if finished_status is None:
            raise Conflict("Processing lease expired; stale worker cannot finish")
        if finished_status == "pending" and _inline_workers_enabled():
            retry = threading.Timer(
                _retry_delay_seconds(job["attempts"]), dispatch, args=(tenant_id, job["id"])
            )
            retry.daemon = True
            retry.start()
        log.info(
            "processing_job_finished tenant_id=%s deal_id=%s job_id=%s kind=%s attempt=%d "
            "outcome=%s durable_status=%s duration_ms=%d queue_wait_ms=%d",
            tenant_id,
            job["deal_id"],
            job["id"],
            job["kind"],
            job["attempts"],
            outcome,
            finished_status,
            round((time.perf_counter() - job_started) * 1000),
            job["queue_wait_ms"],
        )
    return True


_LOCAL_LOCK = threading.Lock()
_LOCAL_PENDING: set[str] = set()
# A LIST, not a set: a tenant may hold several drain threads now, and a set
# would collapse them to one and let `_start_local_workers` start more.
_LOCAL_ACTIVE: list[str] = []


def _local_drain(tenant_id: str) -> None:
    try:
        while _inline_workers_enabled() and run_one(tenant_id):
            pass
    finally:
        with _LOCAL_LOCK:
            _LOCAL_ACTIVE.remove(tenant_id)
            _start_local_workers()


# Drain threads a single tenant may have locally. One was enough while every
# job held the deal exclusively; with parse jobs now independent
# (`EXCLUSIVE_KINDS`) a lone thread is the remaining serialisation, and a folder
# drop is the case that shows it.
LOCAL_THREADS_PER_TENANT = 4
_LOCAL_MAX_THREADS = 8


def _start_local_workers() -> None:
    for tenant in sorted(_LOCAL_PENDING):
        running = sum(1 for t in _LOCAL_ACTIVE if t == tenant)
        if len(_LOCAL_ACTIVE) >= _LOCAL_MAX_THREADS:
            break
        if running >= LOCAL_THREADS_PER_TENANT:
            continue
        # NOT discarded until the tenant has its full complement: the next call
        # needs to know there is still work worth another thread.
        if running + 1 >= LOCAL_THREADS_PER_TENANT:
            _LOCAL_PENDING.discard(tenant)
        _LOCAL_ACTIVE.append(tenant)
        threading.Thread(target=_local_drain, args=(tenant,), daemon=True, name="processing-job").start()


def _inline_workers_enabled() -> bool:
    """Explicit legacy opt-in only; API processes leave durable jobs to workers."""
    return os.getenv("PROCESSING_WORKER_MODE", "external") == "inline" and os.getenv("APP_ENV") in {
        "local",
        "test",
    }


def dispatch(tenant_id: str, job_id: str) -> None:
    """Publish a committed job, or leave it for the legacy durable worker."""
    from creditpaper.queueing.config import migrated_kinds

    migrated = set(migrated_kinds())
    if os.getenv("PROCESSING_QUEUE_BACKEND", "database").lower() == "celery":
        with tenant_cursor(tenant_id) as cur:
            cur.execute(
                "SELECT public.processing_job_kind(%s::uuid,%s::uuid)",
                (tenant_id, job_id),
            )
            row = cur.fetchone()
        if row and row[0] in migrated:
            from creditpaper.queueing import dispatch_job

            dispatch_job(tenant_id, job_id, row[0])
            return
    if _inline_workers_enabled():
        with _LOCAL_LOCK:
            _LOCAL_PENDING.add(tenant_id)
            _start_local_workers()
