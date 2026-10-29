"""Run a bounded-concurrency job worker.

--tenant <uuid>   serve one explicitly assigned tenant
--all-tenants     discover tenants with work (processing_job_tenants(), migration 0022)
                  and run their jobs, each under that tenant's RLS cursor
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
import signal
from threading import Event
from uuid import UUID

log = logging.getLogger("creditpaper.worker")
DEFAULT_CONCURRENCY = 8
DEFAULT_PER_TENANT = 8
IDLE_SLEEP_S = 2.0
LANE_CHOICES = ("all", "generation", "ingestion", "fast", "ocr")


def configure_lane_batches(concurrency: int) -> dict[str, int]:
    """Cap every database micro-batch at the lane's actual compute width."""

    limits: dict[str, int] = {}
    for name, maximum in (
        ("PROCESSING_CLAIM_BATCH", 16),
        ("EXTRACTION_FINALIZE_BATCH", 8),
        ("PROCESSING_FINISH_BATCH", 16),
    ):
        configured = max(1, int(os.getenv(name, "8")))
        value = min(maximum, configured, concurrency)
        os.environ[name] = str(value)
        limits[name] = value
    return limits


def main() -> None:
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--tenant", type=UUID)
    target.add_argument("--all-tenants", action="store_true")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="job concurrency (default: bounded by the worker DB pool)",
    )
    parser.add_argument(
        "--per-tenant",
        type=int,
        default=None,
        help="maximum concurrent jobs for one tenant (bounded by --concurrency)",
    )
    parser.add_argument("--idle-sleep", type=float, default=IDLE_SLEEP_S)
    parser.add_argument("--once", action="store_true")
    # One worker per lane in production: --lane generation and --lane ingestion.
    parser.add_argument("--lane", choices=LANE_CHOICES, default="all")
    args = parser.parse_args()
    if args.idle_sleep <= 0:
        parser.error("--idle-sleep must be positive")
    if args.concurrency is not None and args.concurrency < 1:
        parser.error("--concurrency must be positive")
    if args.per_tenant is not None and args.per_tenant < 1:
        parser.error("--per-tenant must be positive")

    # This must happen before importing database-owning modules: db.py chooses
    # the process-specific pool size at import time.
    os.environ["CREDITPAPER_PROCESS_ROLE"] = "worker"
    from creditpaper.db import close_pool, configured_pool_limits
    from creditpaper.ledger.capacity import validate_worker_capacity
    from creditpaper.ledger.worker import run_all_tenants

    try:
        _, worker_pool_max = configured_pool_limits()
        if args.concurrency is None:
            # Compute concurrency is intentionally independent of DB pool size:
            # jobs release connections before file/OCR/model work and lease
            # renewal is batched once per process.
            args.concurrency = {"ocr": 1, "generation": 2}.get(args.lane, DEFAULT_CONCURRENCY)
        if args.per_tenant is None:
            args.per_tenant = min(DEFAULT_PER_TENANT, args.concurrency)
        capacity = validate_worker_capacity(
            lane=args.lane,
            concurrency=args.concurrency,
            per_tenant=args.per_tenant,
            db_pool_max=worker_pool_max,
        )
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    # A lane must never lease more jobs than it can execute. In particular the
    # single-worker OCR lane previously claimed eight jobs, leaving seven
    # invisible in an in-memory prefetch queue until their leases expired.
    configure_lane_batches(args.concurrency)

    # Import after lane sizing: these modules read batch limits once at import.
    from creditpaper.ledger.jobs import LANES, run_one

    lane_run_one = functools.partial(run_one, kinds=LANES[args.lane])
    stop = Event()

    def request_stop(signum, _frame) -> None:
        log.info("worker_shutdown_requested signal=%s", signum)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info(
        "worker_start lane=%s concurrency=%d per_tenant=%d idle_sleep_s=%.2f "
        "db_pool=%d cpus=%d ocr_workers=%d pdf_processes=%d target=%s",
        args.lane,
        args.concurrency,
        args.per_tenant,
        args.idle_sleep,
        capacity.db_pool_max,
        capacity.cpu_count,
        capacity.ocr_workers,
        capacity.pdf_processes,
        "all" if args.all_tenants else str(args.tenant),
    )
    try:
        # Use the same bounded scheduler for both modes.  The old single-tenant
        # loop called one job at a time, turning a 100-document folder into a
        # serial queue even though the worker pool was available.
        discover = (lambda: [str(args.tenant)]) if args.tenant is not None else None
        options = {
            "concurrency": args.concurrency,
            "per_tenant": args.per_tenant,
            "once": args.once,
            "run_one": lane_run_one,
            "idle_sleep_s": args.idle_sleep,
            "stop": stop,
        }
        if discover is not None:
            options["discover"] = discover
        run_all_tenants(**options)
    finally:
        close_pool()


if __name__ == "__main__":
    main()
