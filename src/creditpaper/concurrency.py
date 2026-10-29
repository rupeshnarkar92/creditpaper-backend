"""The one way this codebase starts threads.

Every parallel stage here — six-wide section generation, six-wide ingest, the
proofreader, the two-wide valuation split — exists to make LLM calls. Those calls
are billed, and their cost is attributed through a `ContextVar` (see
`obs/ledger.py`). `ThreadPoolExecutor` does **not** propagate context into its
workers, so a bare executor silently un-scopes every call inside it: the spend is
real, the ledger is empty, and the report says `$0.00` with no warning.

That is the majority of the spend in a run, so it is not left to memory.
`pool()` copies the submitting thread's context at submit time, and
`tests/test_concurrency.py` fails CI if any module constructs a raw
`ThreadPoolExecutor` — the same choke-point-plus-guard shape as `llm/client.py`,
adopted for the same reason: the failure is invisible in the output.
"""

from __future__ import annotations

import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")


class ContextPreservingExecutor(ThreadPoolExecutor):
    """A `ThreadPoolExecutor` whose workers inherit the submitter's context.

    Each `submit` takes its OWN copy of the context. Sharing one copy across
    submits would raise `cannot enter context: already entered` the moment two
    workers ran at once — the copy is per-task by necessity, not by preference.
    """

    def submit(  # type: ignore[override]
        self, fn: Callable[..., T], /, *args: Any, **kwargs: Any
    ) -> Future[T]:
        ctx = contextvars.copy_context()
        return super().submit(ctx.run, functools.partial(fn, *args, **kwargs))


def pool(max_workers: int, thread_name_prefix: str = "") -> ContextPreservingExecutor:
    """A worker pool that keeps cost attribution intact.

    `max_workers` is clamped to at least 1: several call sites size the pool from
    `len(work)`, and `ThreadPoolExecutor(0)` raises — an empty work list should
    do nothing, not crash the run.
    """
    return ContextPreservingExecutor(max_workers=max(1, max_workers), thread_name_prefix=thread_name_prefix)
