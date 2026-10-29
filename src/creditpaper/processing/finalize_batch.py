"""Process-wide micro-batching for parsed document persistence."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field

from creditpaper.db import tenant_cursor, worker_db_task

log = logging.getLogger(__name__)

_MAX_BATCH = min(8, max(1, int(os.getenv("EXTRACTION_FINALIZE_BATCH", "8"))))
# Folder ingestion is the dominant path. With a remote transaction pooler the
# second parser commonly reaches this queue about two seconds after the first;
# a 1.5-second window consistently flushed singleton writes. A 2.5-second
# bounded window captures that peer while adding at most one extra second to a
# lone document and amortising checkout/commit latency across the batch.
_WINDOW_SECONDS = max(0.0, float(os.getenv("EXTRACTION_FINALIZE_WINDOW_MS", "2500")) / 1000)


@dataclass
class _Request:
    tenant_id: str
    item: dict
    queued_at: float = field(default_factory=time.perf_counter)
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


_QUEUE: list[_Request] = []
_CONDITION = threading.Condition()
_STARTED = False


def _write_batch(tenant_id: str, items: list[dict], queue_wait_ms: int) -> None:
    started = time.perf_counter()
    worker_context = worker_db_task()
    worker_context.__enter__()
    transaction = tenant_cursor(tenant_id)
    exc_info = (None, None, None)
    transaction_entered = False
    checkout_setup_ms = query_ms = 0
    try:
        checkout_started = time.perf_counter()
        cur = transaction.__enter__()
        transaction_entered = True
        checkout_setup_ms = round((time.perf_counter() - checkout_started) * 1000)
        query_started = time.perf_counter()
        cur.execute(
            "SELECT public.finish_document_processing_batch(%s::uuid,%s::jsonb)",
            (tenant_id, json.dumps(items)),
        )
        completed = int(cur.fetchone()[0])
        query_ms = round((time.perf_counter() - query_started) * 1000)
        if completed != len(items):
            raise RuntimeError("Document finalization batch was incomplete")
    except BaseException:
        exc_info = sys.exc_info()
        raise
    finally:
        commit_started = time.perf_counter()
        try:
            if transaction_entered:
                transaction.__exit__(*exc_info)
        finally:
            commit_ms = round((time.perf_counter() - commit_started) * 1000)
            worker_context.__exit__(*exc_info)
        payloads = [item.get("payload", {}) for item in items]
        log.info(
            "document_finalize_db batch_size=%d queue_wait_ms=%d checkout_setup_ms=%d "
            "query_ms=%d commit_ms=%d total_ms=%d page_rows=%d extraction_chunks=%d index_rows=%d",
            len(items),
            queue_wait_ms,
            checkout_setup_ms,
            query_ms,
            commit_ms,
            round((time.perf_counter() - started) * 1000),
            sum(len(payload.get("page_texts") or []) for payload in payloads),
            sum(len(payload.get("chunks") or []) for payload in payloads),
            sum(len(payload.get("index_chunks") or []) for payload in payloads),
        )


def _take_batch() -> list[_Request]:
    global _QUEUE
    first_tenant = _QUEUE[0].tenant_id
    selected = [request for request in _QUEUE if request.tenant_id == first_tenant][:_MAX_BATCH]
    chosen = {id(request) for request in selected}
    _QUEUE = [request for request in _QUEUE if id(request) not in chosen]
    return selected


def _loop() -> None:
    while True:
        with _CONDITION:
            while not _QUEUE:
                _CONDITION.wait()
            deadline = time.monotonic() + _WINDOW_SECONDS
            while len(_QUEUE) < _MAX_BATCH:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                _CONDITION.wait(remaining)
            batch = _take_batch()
        try:
            queue_wait_ms = round(
                max(0.0, time.perf_counter() - min(request.queued_at for request in batch)) * 1000
            )
            _write_batch(batch[0].tenant_id, [request.item for request in batch], queue_wait_ms)
        except BaseException as exc:  # every waiter must be released on DB failure
            for request in batch:
                request.error = exc
        finally:
            for request in batch:
                request.done.set()


def _ensure_started() -> None:
    global _STARTED
    with _CONDITION:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=_loop, daemon=True, name="extraction-finalizer").start()


def finalize_document(tenant_id: str, item: dict) -> None:
    """Persist one parsed document, batching concurrent callers by tenant."""
    _ensure_started()
    request = _Request(tenant_id=tenant_id, item=item)
    with _CONDITION:
        _QUEUE.append(request)
        _CONDITION.notify()
    request.done.wait()
    if request.error is not None:
        raise request.error
