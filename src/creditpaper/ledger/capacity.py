"""Fail-fast worker capacity validation.

The interface accepts deployment numbers and returns one immutable summary.
It performs no I/O, which keeps CLI startup and deployment tests deterministic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerCapacity:
    lane: str
    concurrency: int
    per_tenant: int
    cpu_count: int
    db_pool_max: int
    ocr_workers: int
    pdf_processes: int
    control_plane_connections: int


CONTROL_PLANE_CONNECTIONS = 1


def validate_worker_capacity(
    *,
    lane: str,
    concurrency: int,
    per_tenant: int,
    db_pool_max: int,
    cpu_count: int | None = None,
    ocr_workers: int | None = None,
    pdf_processes: int | None = None,
) -> WorkerCapacity:
    """Validate one worker process against CPU and database safety limits."""
    cpus = max(1, cpu_count or os.cpu_count() or 1)
    ocr = max(1, ocr_workers or int(os.getenv("OCR_WORKERS", "4")))
    pdf = max(1, pdf_processes or int(os.getenv("PDF_EXTRACT_PROCESSES", "4")))
    if lane not in {"all", "ingestion", "fast", "ocr", "generation"}:
        raise ValueError(f"unknown worker lane: {lane}")
    if concurrency < 1 or per_tenant < 1 or db_pool_max < 1:
        raise ValueError("worker concurrency, per-tenant concurrency and DB pool must be positive")
    if per_tenant > concurrency:
        raise ValueError("per-tenant concurrency cannot exceed worker concurrency")
    max_concurrency = max(1, int(os.getenv("CREDITPAPER_WORKER_MAX_CONCURRENCY", str(min(8, cpus)))))
    if concurrency > max_concurrency:
        raise ValueError(
            f"worker concurrency {concurrency} exceeds configured safe maximum {max_concurrency}"
        )
    if lane in {"all", "ingestion", "fast", "ocr"}:
        if ocr > cpus or pdf > cpus:
            raise ValueError(
                f"OCR/PDF pools ({ocr}/{pdf}) exceed available CPUs ({cpus}); "
                "lower OCR_WORKERS or PDF_EXTRACT_PROCESSES"
            )
        cpu_helpers = ocr if lane in {"all", "ingestion", "ocr"} else pdf
        if concurrency + cpu_helpers > cpus * 2:
            raise ValueError(f"document + OCR concurrency ({concurrency}+{ocr}) oversubscribes {cpus} CPUs")
    return WorkerCapacity(
        lane,
        concurrency,
        per_tenant,
        cpus,
        db_pool_max,
        ocr,
        pdf,
        CONTROL_PLANE_CONNECTIONS,
    )
