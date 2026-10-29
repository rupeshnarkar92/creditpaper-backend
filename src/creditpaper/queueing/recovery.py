"""Recover PostgreSQL-owned jobs after a broker or worker interruption."""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque

from creditpaper.queueing.config import migrated_kinds

log = logging.getLogger(__name__)


def recovery_batch_size() -> int:
    """Bound broker replay so recovery cannot swamp normal API traffic."""
    return max(1, min(250, int(os.getenv("CELERY_RECOVERY_BATCH_SIZE", "25"))))


def recover_pending(*, per_tenant_limit: int | None = None, kinds: list[str] | None = None) -> dict[str, int]:
    """Republish due pending and expired-running jobs for every tenant."""
    from creditpaper.db import tenant_cursor
    from creditpaper.ledger.worker import discover_tenants
    from creditpaper.queueing.dispatch import DispatchOutcome, dispatch_job_outcome

    limit = recovery_batch_size() if per_tenant_limit is None else max(1, per_tenant_limit)
    found = published = failed = deferred = duplicate = 0
    # One deque per tenant/lane. Round-robin over these groups so a large parse
    # backlog neither hides another lane nor another tenant. Once a lane says it
    # is full, skip the rest of that group for this recovery pass.
    pending: deque[tuple[str, str, deque]] = deque()
    for tenant_id in discover_tenants():
        with tenant_cursor(tenant_id) as cur:
            cur.execute(
                "SELECT public.pending_processing_jobs_for_delivery(%s::uuid,%s::text[],%s)",
                (tenant_id, migrated_kinds() if kinds is None else kinds, limit),
            )
            jobs = cur.fetchone()[0]
        found += len(jobs)
        by_kind: dict[str, deque] = defaultdict(deque)
        for job in jobs:
            by_kind[job["kind"]].append(job)
        for kind, grouped in by_kind.items():
            pending.append((tenant_id, kind, grouped))

    while pending:
        tenant_id, kind, jobs = pending.popleft()
        job = jobs.popleft()
        outcome = dispatch_job_outcome(tenant_id, job["id"], job["kind"])
        if outcome is DispatchOutcome.PUBLISHED:
            published += 1
        elif outcome is DispatchOutcome.ALREADY_DELIVERED:
            duplicate += 1
        elif outcome is DispatchOutcome.DEFERRED:
            deferred += 1
            # Every later job in this tenant/lane would hit the same bounded
            # capacity. Leave them durable for completion wake-up/next pass.
            continue
        else:
            failed += 1
        if jobs:
            pending.append((tenant_id, kind, jobs))
    return {
        "found": found,
        "published": published,
        "duplicate": duplicate,
        "deferred": deferred,
        "failed": failed,
    }


def recover_on_startup(client, worker_key: str, *, attempts: int = 6) -> None:
    """Run bounded startup recovery, retrying transient DB/broker failures."""
    for attempt in range(1, attempts + 1):
        lock_key = "creditpaper:recovery:startup"
        if not client.set(lock_key, worker_key, nx=True, ex=30):
            return
        try:
            result = recover_pending()
            log.info("celery_startup_recovery %s", result)
            return
        except Exception as exc:
            log.warning(
                "celery_startup_recovery_failed attempt=%d error=%s",
                attempt,
                type(exc).__name__,
            )
            # Release only our own short-lived coordination lock. If another
            # worker replaced it after expiry, leave that worker's lock intact.
            if client.get(lock_key) == worker_key:
                client.delete(lock_key)
            if attempt < attempts:
                time.sleep(min(10, attempt * 2))
