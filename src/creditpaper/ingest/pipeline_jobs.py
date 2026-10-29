"""One window of work: retried, then fallen back, then parked.

A document is too big to be the unit of failure. A 400-page scanned bundle that
fails on page 380 must not re-read pages 1-379, and one window that a provider
refuses must not cost the deal the other eleven.

Durability is bought where it pays. Windows run in-process on
`concurrency.pool`, which copies the submitting thread's context so the cost
ledger still attributes the spend - a bare ThreadPoolExecutor does not, and that
defect has already been shipped once here. A document whose plan exceeds
DURABLE_WINDOW_THRESHOLD fans out as queue jobs instead, so a worker restart
resumes rather than re-reads. A durable row per window is the right trade for a
bundle and pure overhead for a three-page terms sheet.

Parking is the end of the line, not a silent drop: the unit keeps its whole
attempt history so a person can see what was tried.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Sequence

from pydantic import BaseModel

from creditpaper.concurrency import pool
from creditpaper.ingest.windows import Window

log = logging.getLogger(__name__)

# Per page, never per document: p99 is about 1 s on plan and CAD pages, and a
# document-level timeout would let one bad page consume the whole budget.
PAGE_TIMEOUT_S = 15
DURABLE_WINDOW_THRESHOLD = 8
_BACKOFF_S = (0.5, 2.0, 5.0)


class Attempt(BaseModel):
    step: str
    ok: bool
    error: str | None = None


class StepOutcome(BaseModel):
    ok: bool
    attempts: list[Attempt]
    parked: bool


def job_key(content_hash: str, extractor_version: str, window: str) -> str:
    """Idempotency key for one unit of work.

    The same bytes, read by the same extractor version, for the same window, is
    the same work - so a re-opened session resumes instead of re-running.
    """
    raw = f"{content_hash}|{extractor_version}|{window}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def run_with_fallback(
    steps: Sequence[tuple[str, Callable[[], object]]],
    *,
    retries: int = 2,
    sleep: Callable[[float], None] = time.sleep,
) -> StepOutcome:
    attempts: list[Attempt] = []
    for name, step in steps:
        for attempt in range(retries + 1):
            try:
                step()
                attempts.append(Attempt(step=name, ok=True))
                return StepOutcome(ok=True, attempts=attempts, parked=False)
            except Exception as exc:  # noqa: BLE001 - the error is the finding
                attempts.append(Attempt(step=name, ok=False, error=type(exc).__name__))
                if attempt < retries:
                    sleep(_BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)])
    log.warning("ingest unit parked after %d attempts", len(attempts))
    return StepOutcome(ok=False, attempts=attempts, parked=True)


def run_windows(
    windows: Sequence[Window],
    work: Callable[[Window], object],
    *,
    max_workers: int = 6,
) -> list[StepOutcome]:
    """Run every window, and let each fail on its own.

    pool(), not a bare executor: these call the model, and cost attribution
    travels in a ContextVar that a raw ThreadPoolExecutor drops.
    """
    if not windows:
        return []
    with pool(min(max_workers, len(windows)), thread_name_prefix="ingest-window") as executor:
        futures = [
            executor.submit(
                run_with_fallback,
                [(f"window:{w.group}", lambda w=w: work(w))],
            )
            for w in windows
        ]
        return [f.result() for f in futures]


def should_go_durable(windows: Sequence[Window]) -> bool:
    return len(windows) > DURABLE_WINDOW_THRESHOLD


def enqueue_windows(
    tenant_id: str,
    deal_id: str,
    *,
    content_hash: str,
    extractor_version: str,
    windows: Sequence[Window],
) -> list[str]:
    """One durable job per window, keyed so a restart resumes rather than re-reads."""
    from creditpaper.ledger import jobs

    job_ids: list[str] = []
    for window in windows:
        key = job_key(content_hash, extractor_version, f"{window.group}:{window.pages}")
        job_id = jobs.enqueue(
            tenant_id,
            deal_id,
            "ingest_window",
            key,
            {
                "content_hash": content_hash,
                "extractor_version": extractor_version,
                "group": window.group,
                "pages": list(window.pages),
            },
        )
        jobs.dispatch(tenant_id, job_id)
        job_ids.append(job_id)
    return job_ids
