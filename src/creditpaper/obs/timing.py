"""Content-free timings, propagated through the existing context-aware pool."""

from __future__ import annotations

import logging
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from uuid import uuid4

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
# Uvicorn does not configure the root logger. Give these content-free records
# their own console handler so timings are visible without a debug logging mode.
if not log.handlers:
    log.addHandler(logging.StreamHandler())


@dataclass
class ProcessingTiming:
    run_id: str = field(default_factory=lambda: uuid4().hex)
    totals: dict[str, float] = field(default_factory=dict)
    llm_calls: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


_active: ContextVar[ProcessingTiming | None] = ContextVar("processing_timing", default=None)


def timed(stage: str):
    """Log only fixed stage names, random run IDs, durations and counters."""

    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            scope = _active.get()
            token = None
            if scope is None:
                scope = ProcessingTiming()
                token = _active.set(scope)
            started = time.perf_counter()
            ok = False
            try:
                result = fn(*args, **kwargs)
                ok = True
                return result
            finally:
                elapsed = time.perf_counter() - started
                with scope.lock:
                    scope.totals[stage] = scope.totals.get(stage, 0.0) + elapsed
                    if stage == "llm":
                        scope.llm_calls += 1
                    count = scope.llm_calls
                log.info(
                    "processing_timing run=%s stage=%s elapsed_s=%.6f ok=%s llm_calls=%d",
                    scope.run_id,
                    stage,
                    elapsed,
                    ok,
                    count,
                )
                if token is not None:
                    log.info(
                        "processing_total run=%s total_s=%.6f pdf_parsing_s=%.6f "
                        "llm_calls=%d total_llm_s=%.6f validation_s=%.6f reconciliation_s=%.6f",
                        scope.run_id,
                        elapsed,
                        scope.totals.get("pdf_parsing", 0.0),
                        count,
                        scope.totals.get("llm", 0.0),
                        scope.totals.get("validation", 0.0),
                        scope.totals.get("reconciliation", 0.0),
                    )
                    _active.reset(token)

        return wrapped

    return decorate
