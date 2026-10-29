"""Publish durable PostgreSQL jobs to the selected execution backend."""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from enum import StrEnum
from uuid import uuid4

from redis import Redis

from creditpaper.queueing.config import broker_url, migrated_kinds, queue_for_kind

log = logging.getLogger(__name__)


def _delivery_key(job_id: str) -> str:
    return f"creditpaper:delivery:{job_id}"


def _capacity_key(tenant_id: str, kind: str) -> str:
    return f"creditpaper:deliveries:{tenant_id}:{queue_for_kind(kind)}"


class DeliveryCapacityFull(RuntimeError):
    """Work stays durable in PostgreSQL until broker capacity is released."""


class DispatchOutcome(StrEnum):
    """Why a durable job was (or was not) handed to the broker."""

    PUBLISHED = "published"
    ALREADY_DELIVERED = "already_delivered"
    DEFERRED = "deferred"
    FAILED = "failed"


def _claim_delivery(job_id: str, tenant_id: str, kind: str) -> str | None:
    """Reserve one broker delivery while its durable job is pending.

    Recovery runs periodically on every installation. Without this short Redis
    marker, the same pending PostgreSQL row is appended to the broker on every
    pass even though Celery task ids are not queue-level deduplication keys.
    The marker expires so a broker flush or lost publish remains recoverable.
    """
    # This marker covers broker wait and is renewed every ten seconds during
    # execution. A short bounded default therefore recovers a lost worker
    # promptly without expiring underneath long OCR/model work.
    ttl = max(60, int(os.getenv("CELERY_DELIVERY_DEDUPE_SECONDS", "120")))
    client = Redis.from_url(broker_url(), decode_responses=True)
    token = str(uuid4())
    now = time.time()
    result = client.eval(
        """
        if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
        redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])
        if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[3]) then return -1 end
        redis.call('SET', KEYS[1], ARGV[4], 'EX', ARGV[5])
        redis.call('ZADD', KEYS[2], ARGV[2], ARGV[4])
        redis.call('EXPIRE', KEYS[2], ARGV[5])
        return 1
        """,
        2,
        _delivery_key(job_id),
        _capacity_key(tenant_id, kind),
        now,
        now + ttl,
        dispatch_queue_limit(kind),
        token,
        ttl,
    )
    if result == -1:
        raise DeliveryCapacityFull()
    return token if result else None


def _release_delivery(job_id: str, token: str, tenant_id: str, kind: str) -> None:
    Redis.from_url(broker_url(), decode_responses=True).eval(
        "if redis.call('GET',KEYS[1]) == ARGV[1] then "
        "redis.call('ZREM',KEYS[2],ARGV[1]); return redis.call('DEL',KEYS[1]) end return 0",
        2,
        _delivery_key(job_id),
        _capacity_key(tenant_id, kind),
        token,
    )


def _renew_delivery(job_id: str, token: str, tenant_id: str, kind: str) -> bool:
    ttl = max(60, int(os.getenv("CELERY_DELIVERY_DEDUPE_SECONDS", "120")))
    return bool(
        Redis.from_url(broker_url(), decode_responses=True).eval(
            "if redis.call('GET',KEYS[1]) ~= ARGV[1] then return 0 end "
            "redis.call('EXPIRE',KEYS[1],ARGV[2]); "
            "redis.call('ZADD',KEYS[2],ARGV[3],ARGV[1]); "
            "redis.call('EXPIRE',KEYS[2],ARGV[2]); return 1",
            2,
            _delivery_key(job_id),
            _capacity_key(tenant_id, kind),
            token,
            ttl,
            time.time() + ttl,
        )
    )


@contextmanager
def delivery_lease(job_id: str, token: str | None, tenant_id: str, kind: str):
    """Keep queued/running admission owned while long OCR or model work runs."""
    stop = threading.Event()

    def renew():
        while not stop.wait(10):
            try:
                if not _renew_delivery(job_id, token, tenant_id, kind):
                    return
            except Exception as exc:
                log.warning("delivery_renew_failed job_id=%s error=%s", job_id, type(exc).__name__)

    if token:
        threading.Thread(target=renew, name="delivery-renewal", daemon=True).start()
    try:
        yield
    finally:
        stop.set()


def backend() -> str:
    return os.getenv("PROCESSING_QUEUE_BACKEND", "database").strip().lower()


def dispatch_queue_limit(kind: str) -> int:
    """Bound one tenant in each broker lane before tasks occupy workers.

    Lane-specific overrides use e.g. ``CELERY_DISPATCH_PARSE_CONCURRENCY``.
    One item per lane is deliberately conservative: it keeps the tenant-wide
    execution ceiling useful while preventing a parse backlog from filling a
    lane with duplicate queued deliveries.
    """
    queue = queue_for_kind(kind)
    suffix = queue.rsplit(".", 1)[-1].replace("-", "_").upper()
    configured = os.getenv(f"CELERY_DISPATCH_{suffix}_CONCURRENCY")
    if configured:
        return max(1, int(configured))
    generic = os.getenv("CELERY_DISPATCH_PER_TENANT_QUEUE")
    if generic:
        return max(1, int(generic))
    tenant_ceiling = max(1, int(os.getenv("CELERY_TENANT_CONCURRENCY", "2")))
    # Keep one aggregate tenant slot unavailable to brokered parse deliveries;
    # intake has its own lane cap and can always use the remaining slot.
    return max(1, tenant_ceiling - 1) if suffix == "PARSE" else 1


def dispatch_job_outcome(tenant_id: str, job_id: str, kind: str) -> DispatchOutcome:
    """Deliver a durable job and retain the reason when it is not published."""
    if backend() != "celery":
        return DispatchOutcome.FAILED
    from creditpaper.queueing.tasks import execute_processing_job

    queue = queue_for_kind(kind)
    reservation = None
    try:
        reservation = _claim_delivery(job_id, tenant_id, kind)
        if not reservation:
            return DispatchOutcome.ALREADY_DELIVERED
        execute_processing_job.apply_async(
            kwargs={"tenant_id": tenant_id, "job_id": job_id, "kind": kind, "delivery_token": reservation},
            queue=queue,
            task_id=f"processing:{job_id}",
        )
    except DeliveryCapacityFull:
        return DispatchOutcome.DEFERRED
    except Exception as exc:  # broker delivery is recoverable from PostgreSQL
        try:
            if reservation:
                _release_delivery(job_id, reservation, tenant_id, kind)
        except Exception:
            pass
        log.error("processing_publish_failed job_id=%s kind=%s error=%s", job_id, kind, type(exc).__name__)
        return DispatchOutcome.FAILED
    return DispatchOutcome.PUBLISHED


def dispatch_job(tenant_id: str, job_id: str, kind: str) -> bool:
    """Deliver a durable job id.

    A broker outage does not erase work: the processing_jobs row already
    committed. Returning False lets callers log/alert while a recovery command
    can republish durable pending rows after Redis returns.
    """
    return dispatch_job_outcome(tenant_id, job_id, kind) in {
        DispatchOutcome.PUBLISHED,
        DispatchOutcome.ALREADY_DELIVERED,
    }


def resume_deal(tenant_id: str, deal_id: str, *, limit: int = 1000) -> dict[str, int | bool]:
    """Idempotently redeliver a deal's due durable jobs to live workers."""
    from creditpaper.db import tenant_cursor
    from creditpaper.queueing.health import workers_alive

    alive = workers_alive()
    if not alive:
        return {"worker_alive": False, "due": 0, "published": 0}
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.pending_deal_jobs_for_delivery(%s::uuid,%s,%s::text[],%s)",
            (tenant_id, deal_id, migrated_kinds(), limit),
        )
        jobs = cur.fetchone()[0]
    published = sum(int(dispatch_job(tenant_id, job["id"], job["kind"])) for job in jobs)
    return {"worker_alive": True, "due": len(jobs), "published": published}


def dispatch_due(tenant_id: str) -> None:
    """Wake durable successors after completion commits and capacity is freed.

    The pending PostgreSQL job is the durable delivery intent. A process death
    between commit and publish is repaired by recovery; normal completion does
    not wait for the recovery interval.
    """
    from creditpaper.db import tenant_cursor
    from creditpaper.queueing.recovery import recovery_batch_size

    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.pending_processing_jobs_for_delivery(%s::uuid,%s::text[],%s)",
            (tenant_id, migrated_kinds(), recovery_batch_size()),
        )
        due = cur.fetchone()[0]
    # Interleave lanes and stop probing a lane once its bounded broker capacity
    # is full. A flat scan needlessly attempted every blocked parse row after
    # each completion.
    from collections import defaultdict, deque

    by_kind: dict[str, deque] = defaultdict(deque)
    for job in due:
        by_kind[job["kind"]].append(job)
    lanes = deque((kind, jobs) for kind, jobs in by_kind.items())
    while lanes:
        kind, jobs = lanes.popleft()
        job = jobs.popleft()
        outcome = dispatch_job_outcome(tenant_id, job["id"], kind)
        if outcome is DispatchOutcome.DEFERRED:
            continue
        if jobs:
            lanes.append((kind, jobs))
