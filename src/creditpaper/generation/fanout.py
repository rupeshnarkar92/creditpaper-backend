"""Run one stage's calls in parallel, after a single warm-up call.

Parallel calls on the same prompt prefix all pay the cache-write price: a cache
entry is readable only once the first response has started. So the first task
runs alone, then the rest fan out and read the warm cache (spec §12). Each task
yields an Outcome, so one domain failing never discards its siblings' results —
the caller decides how to degrade (spec §12 error table).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from creditpaper.concurrency import pool

R = TypeVar("R")


@dataclass(frozen=True)
class Outcome(Generic[R]):
    value: R | None = None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _attempt(task: Callable[[], R]) -> Outcome[R]:
    try:
        return Outcome(value=task())
    except Exception as exc:  # noqa: BLE001 — contained per task by design
        return Outcome(error=exc)


def fan_out(
    tasks: Sequence[Callable[[], R]], *, max_workers: int = 6, warm_first: bool = True
) -> list[Outcome[R]]:
    if not tasks:
        return []
    results: list[Outcome[R] | None] = [None] * len(tasks)
    start = 0
    if warm_first:
        results[0] = _attempt(tasks[0])
        start = 1
    rest = list(enumerate(tasks))[start:]
    if rest:
        with pool(max_workers=min(max_workers, len(rest)), thread_name_prefix="generation") as executor:
            futures = [(i, executor.submit(_attempt, task)) for i, task in rest]
            for i, future in futures:
                results[i] = future.result()
    return [r for r in results if r is not None]
