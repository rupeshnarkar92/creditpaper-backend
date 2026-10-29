"""Process-wide micro-batching for fenced durable job completion."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

from creditpaper.db import tenant_cursor, worker_db_task

_MAX_BATCH = min(16, max(1, int(os.getenv("PROCESSING_FINISH_BATCH", "8"))))
_WINDOW_SECONDS = max(0.0, float(os.getenv("PROCESSING_FINISH_WINDOW_MS", "500")) / 1000)


@dataclass
class _Request:
    tenant_id: str
    item: dict
    done: threading.Event = field(default_factory=threading.Event)
    status: str | None = None
    error: BaseException | None = None


_QUEUE: list[_Request] = []
_CONDITION = threading.Condition()
_STARTED = False


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
            with worker_db_task(), tenant_cursor(batch[0].tenant_id) as cur:
                cur.execute(
                    "SELECT public.finish_processing_jobs_observed_batch(%s::uuid,%s::jsonb)",
                    (batch[0].tenant_id, json.dumps([request.item for request in batch])),
                )
                statuses = {row["id"]: row["status"] for row in cur.fetchone()[0]}
            for request in batch:
                request.status = statuses.get(request.item["id"])
        except BaseException as exc:
            for request in batch:
                request.error = exc
        finally:
            for request in batch:
                request.done.set()


def finish_job(tenant_id: str, item: dict) -> str | None:
    global _STARTED
    with _CONDITION:
        if not _STARTED:
            _STARTED = True
            threading.Thread(target=_loop, daemon=True, name="processing-job-finisher").start()
        request = _Request(tenant_id, item)
        _QUEUE.append(request)
        _CONDITION.notify()
    request.done.wait()
    if request.error is not None:
        raise request.error
    return request.status
