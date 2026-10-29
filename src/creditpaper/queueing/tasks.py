"""Celery tasks that execute PostgreSQL-owned workflow records."""

from __future__ import annotations

import logging
import random
import threading
import time

from celery import Task  # type: ignore[import-untyped]
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from creditpaper.queueing.celery_app import app
from creditpaper.queueing.config import queue_for_kind
from creditpaper.queueing.limits import (
    SlotUnavailable,
    lease_seconds,
    limiter,
    llm_limit,
    tenant_execution_limit,
    tenant_queue_limit,
)

log = logging.getLogger(__name__)
_LLM_KINDS = {
    "generation",
    "document",
    "extraction",
    "corpus_extraction",
    "ingest_window",
    "ingest_speculate",
    "ingest_propose",
}


@app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="creditpaper.execute_processing_job",
    autoretry_for=(),
    max_retries=None,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=3600,
    time_limit=3900,
)
def execute_processing_job(
    self: Task, *, tenant_id: str, job_id: str, kind: str, delivery_token: str | None = None
) -> dict[str, int | str | bool]:
    """Execute one durable job with tenant and optional global LLM limits."""
    started = time.perf_counter()
    task_id = str(self.request.id or job_id)
    # Celery deliberately keeps the same task id across retries and recovery
    # republishes. A delivery-specific token makes the Redis job slot a real
    # mutex: duplicate broker messages cannot execute the same durable row at
    # the same time, while a retry can acquire it after the prior call exits.
    token = f"{task_id}:{threading.get_ident()}:{time.time_ns()}"
    slots = limiter()
    lease_s = lease_seconds()
    try:
        with slots.slot(f"job:{job_id}", token, 1, lease_s):
            with slots.slot(f"tenant:{tenant_id}", token, tenant_execution_limit(tenant_id, kind), lease_s):
                tenant_queue_scope = f"tenant_queue:{tenant_id}:{queue_for_kind(kind)}"
                with slots.slot(tenant_queue_scope, token, tenant_queue_limit(kind), lease_s):
                    if kind in _LLM_KINDS:
                        with slots.slot("llm", token, llm_limit(), lease_s):
                            ran = _run_with_delivery(tenant_id, job_id, task_id, kind, delivery_token)
                    else:
                        ran = _run_with_delivery(tenant_id, job_id, task_id, kind, delivery_token)
    except SlotUnavailable as exc:
        # Capacity rejection is admission control, not a processing failure.
        # Retrying the broker message every few seconds created a retry storm.
        # Release delivery admission and leave PostgreSQL as durable intent;
        # completion wake-up or bounded recovery will publish it later.
        from creditpaper.queueing.dispatch import _release_delivery

        if delivery_token:
            try:
                _release_delivery(job_id, delivery_token, tenant_id, kind)
            except Exception as release_exc:
                log.warning(
                    "capacity_delivery_release_failed job_id=%s error=%s",
                    job_id,
                    type(release_exc).__name__,
                )
        log.info(
            "celery_job_deferred tenant_id=%s job_id=%s kind=%s scope=%s",
            tenant_id,
            job_id,
            kind,
            exc.scope,
        )
        return {"ran": False, "status": "deferred", "duration_ms": 0}
    except (OperationalError, PoolTimeout) as exc:
        # Database pressure is transient and must not turn a durable job into a
        # failed Celery delivery. Back off so API requests and other tenants get
        # a chance to acquire one of the deliberately small connection pools.
        log.warning(
            "celery_job_db_retry tenant_id=%s job_id=%s kind=%s error=%s",
            tenant_id,
            job_id,
            kind,
            type(exc).__name__,
        )
        raise self.retry(countdown=random.uniform(5.0, 12.0), exc=exc) from exc

    from creditpaper.db import worker_db_task
    from creditpaper.ledger.jobs import status_for_job

    try:
        # Status and successor discovery are worker DB work too. Keeping them
        # outside the worker admission semaphore made concurrent Celery task
        # epilogues race a one-connection pool and retry after otherwise
        # successful processing.
        with worker_db_task():
            durable = status_for_job(tenant_id, job_id)
    except (OperationalError, PoolTimeout) as exc:
        log.warning(
            "celery_status_db_retry tenant_id=%s job_id=%s kind=%s error=%s",
            tenant_id,
            job_id,
            kind,
            type(exc).__name__,
        )
        raise self.retry(countdown=random.uniform(5.0, 12.0), exc=exc) from exc
    if durable["status"] == "pending":
        if not ran and durable.get("retry_in_seconds", 0) <= 0:
            # Dependency-blocked jobs stay durable. Free their admission slot;
            # predecessor completion will publish them when eligible.
            if delivery_token:
                from creditpaper.queueing.dispatch import _release_delivery

                _release_delivery(job_id, delivery_token, tenant_id, kind)
            return {"ran": False, "status": "waiting", "duration_ms": 0}
        delay = max(1.0, min(60.0, durable.get("retry_in_seconds", 2.0)))
        raise self.retry(countdown=delay)
    if durable["status"] in {"complete", "failed"}:
        from creditpaper.queueing.dispatch import _release_delivery, dispatch_due

        try:
            if delivery_token:
                _release_delivery(job_id, delivery_token, tenant_id, kind)
            with worker_db_task():
                dispatch_due(tenant_id)
        except Exception as exc:
            log.warning("delivery_release_failed job_id=%s error=%s", job_id, type(exc).__name__)
    duration_ms = round((time.perf_counter() - started) * 1000)
    log.info(
        "celery_job_finished tenant_id=%s job_id=%s kind=%s status=%s duration_ms=%d",
        tenant_id,
        job_id,
        kind,
        durable["status"],
        duration_ms,
    )
    return {"ran": ran, "status": durable["status"], "duration_ms": duration_ms}


def _run_with_delivery(tenant_id, job_id, task_id, kind, delivery_token):
    from creditpaper.queueing.dispatch import delivery_lease

    with delivery_lease(job_id, delivery_token, tenant_id, kind):
        return _run(tenant_id, job_id, task_id)


def _run(tenant_id: str, job_id: str, task_id: str) -> bool:
    from creditpaper.db import worker_db_task
    from creditpaper.ledger.jobs import run_one

    with worker_db_task():
        return bool(
            run_one(
                tenant_id,
                job_id,
                renew_lease=True,
                broker_task_id=task_id,
            )
        )
