"""The all-tenants job worker: find which tenants have work, then run it under RLS.

`processing_job_tenants()` (migration 0022) returns tenant ids only. Each pass offers every
tenant a slot before offering any tenant a second, so one busy tenant cannot starve the
others while a folder drop still parses in parallel,
and every claim still goes through `jobs.run_one`, which opens a tenant cursor.
Used by `scripts/process_jobs.py --all-tenants`; a hosted worker needs no code change.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Executor, wait
from threading import Event

from creditpaper.concurrency import pool
from creditpaper.db import cursor, tenant_cursor, tenant_uuid, worker_db_task

log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 8

# How many jobs of ONE tenant may run at once. Bounded, because the pool is
# shared: a tenant is offered its n-th slot only after every other tenant
# has been offered its (n-1)-th.
DEFAULT_PER_TENANT = 8
IDLE_SLEEP_S = 2.0


def discover_tenants() -> list[str]:
    """Tenant ids with a due job or an expired lease. No job data leaves the database."""
    with cursor() as cur:
        cur.execute("SELECT tenant_id FROM processing_job_tenants() AS t(tenant_id)")
        rows = cur.fetchall()
    out: list[str] = []
    for (tenant_id,) in rows:
        try:
            out.append(str(tenant_uuid(str(tenant_id))))
        except ValueError:
            continue
    result = sorted(set(out))
    for tenant_id in result:
        _touch_heartbeat(tenant_id)
    return result


def _touch_heartbeat(tenant_id: str) -> None:
    """Record that this worker is alive for a tenant. Failure never stops discovery."""
    try:
        with tenant_cursor(tenant_id) as cur:
            cur.execute("SELECT public.touch_worker_heartbeat(%s::uuid)", (tenant_id,))
    except Exception as exc:  # noqa: BLE001 — a heartbeat outage must not stop job discovery
        log.warning("worker: heartbeat write failed (%s)", type(exc).__name__)


def _run_tenant(run_one: Callable[[str], bool], tenant_id: str) -> bool:
    try:
        with worker_db_task():
            return bool(run_one(tenant_id))
    except Exception as exc:  # noqa: BLE001 — one tenant's failure never stops the worker
        error_type = type(exc).__name__
        if error_type == "PoolTimeout":
            log.warning(
                "worker: DB pool exhausted while running a tenant job; "
                "restart with lower --concurrency or a safely budgeted larger worker pool"
            )
        else:
            log.warning("worker: job run failed for a tenant (%s)", error_type)
        return False


def run_pass(
    executor: Executor,
    *,
    discover: Callable[[], list[str]] = discover_tenants,
    run_one: Callable[[str], bool] | None = None,
    per_tenant: int = DEFAULT_PER_TENANT,
) -> bool:
    """One pass: up to `per_tenant` jobs for each tenant with work.

    ROUND-ROBIN, not tenant-at-a-time. Every tenant is offered its first slot
    before any tenant is offered a second, so a tenant with a 144-document
    folder cannot take the pool while another waits — the property the original
    one-job-per-tenant rule was protecting.

    It was protecting it too well. One job per tenant per pass meant an analyst
    dropping a folder had its documents parsed strictly one after another, and
    with `parse` also holding the deal exclusively (see `jobs.EXCLUSIVE_KINDS`)
    nothing could overlap. 144 documents at ~1.2s each is 180 seconds before a
    single fact is extracted, and a folder drop is the production case.

    True when any job ran.
    """
    if run_one is None:
        from creditpaper.ledger.jobs import run_one as run_one_job

        run_one = run_one_job
    tenants = discover()
    if not tenants:
        return False
    futures = [
        executor.submit(_run_tenant, run_one, tenant) for _ in range(max(1, per_tenant)) for tenant in tenants
    ]
    # Every future is waited on: a pass that returned while work was still
    # running would let the loop sleep with jobs in flight.
    return any([f.result() for f in futures])


def run_all_tenants(
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    per_tenant: int = DEFAULT_PER_TENANT,
    once: bool = False,
    idle_sleep_s: float = IDLE_SLEEP_S,
    discover: Callable[[], list[str]] = discover_tenants,
    run_one: Callable[[str], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    stop: Event | None = None,
) -> None:
    """Refill free slots independently; stop drains in-flight jobs without new claims.

    Bounds apply per process. Deployments must budget connections across replicas.
    Discovery and empty-queue retries are paced, not repeated for every completion.
    """
    if concurrency < 1 or per_tenant < 1 or idle_sleep_s <= 0:
        raise ValueError("concurrency, per_tenant and idle_sleep_s must be positive")
    if run_one is None:
        from creditpaper.ledger.jobs import run_one as run_one_job

        run_one = run_one_job
    with pool(concurrency, thread_name_prefix="processing-worker") as executor:
        if once:
            run_pass(executor, discover=discover, run_one=run_one, per_tenant=per_tenant)
            return
        pending = {}
        active = Counter()
        tenants = deque()
        retry_after = {}
        next_discovery = 0.0
        while stop is None or not stop.is_set():
            now = time.monotonic()
            if now >= next_discovery:
                try:
                    found = list(dict.fromkeys(discover()))
                    # Preserve rotation when discovery returns a stable sorted list.
                    tenants = deque(
                        [t for t in tenants if t in found] + [t for t in found if t not in tenants]
                    )
                    retry_after = {t: deadline for t, deadline in retry_after.items() if t in found}
                except Exception as exc:  # noqa: BLE001 — discovery outage must not strand running jobs
                    log.warning("worker: tenant discovery failed (%s)", type(exc).__name__)
                    tenants.clear()
                next_discovery = time.monotonic() + idle_sleep_s
            while len(pending) < concurrency and (stop is None or not stop.is_set()):
                selected = None
                for _ in range(len(tenants)):
                    tenant = tenants.popleft()
                    tenants.append(tenant)
                    if active[tenant] < per_tenant and retry_after.get(tenant, 0) <= now:
                        selected = tenant
                        break
                if selected is None:
                    break
                pending[executor.submit(_run_tenant, run_one, selected)] = selected
                active[selected] += 1
            if pending:
                done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    tenant = pending.pop(future)
                    active[tenant] -= 1
                    if not active[tenant]:
                        del active[tenant]
                    if not future.result():
                        retry_after[tenant] = time.monotonic() + idle_sleep_s
            elif stop is not None:
                stop.wait(min(idle_sleep_s, 0.2))
            else:
                sleep(idle_sleep_s)
