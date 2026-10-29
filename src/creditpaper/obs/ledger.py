"""Cost accounting: pricing, usage records, and the per-run ledger.

Split from the LLM transport (`creditpaper.llm.client`) 2026-08-21. The two lived
in one module named for observability, so every agent and extractor imported its
LLM *transport* from `obs.cost` — a module whose name says telemetry. The
dependency runs one way: the transport records into the ledger; the ledger knows
nothing about how a call is made.

The separation matters beyond tidiness. These types are what the run manifest the
architecture spec requires is built from — model ids, per-stage effort, cost and
timings, recorded so two differing papers can be explained by diffing manifests.
That is telemetry crossing the engine/platform seam, whereas the transport is
purely internal.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date

# USD per 1M tokens: (input, output). Cache write/read derived via the multipliers
# below. Keep in step with the claude-api model table. Legacy entries stay so old
# ledgers reprice correctly.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Sonnet 5 ships at an introductory rate through 2026-08-31. Report true spend
# now and true list price after, rather than baking the promotional number into
# the per-paper price.
_INTRO_PRICES: dict[str, tuple[tuple[float, float], date]] = {
    "claude-sonnet-5": ((2.0, 10.0), date(2026, 8, 31)),
}

_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10
# Cache reads are not 0.1x everywhere: Opus 5.5 reads at $0.20 against $4.00 input.
_CACHE_READ_MULT_BY_KEY: dict[str, float] = {"claude-opus-5-5": 0.05}

# US-pinned inference bills at 1.1x across every token category. Only "us" and
# "global" exist — there is no AU/APAC geo — and the parameter requires Claude
# 4.6+, so it must never be sent on Haiku 4.5 or older (400).
_US_GEO_MULT = 1.1


def price_key(model: str) -> str | None:
    """The PRICES key that governs `model`, by LONGEST matching prefix.

    Anthropic recommends pinning a dated model id in production
    (`claude-opus-5-20260714`), and an exact-match lookup on the bare alias
    returns nothing for it — so a correctly-pinned production run priced every
    call at $0.00 and reported a total the product's price would then be set
    from. The only signal was a WARNING line inside a text blob.

    Longest prefix, not first match: `claude-opus-4-8` and `claude-opus-4-8-mini`
    would both prefix-match a hypothetical id, and the more specific entry is the
    right one. Anchored at the start, so an id cannot pick up a price by
    containing another model's name somewhere in the middle.
    """
    if model in PRICES:
        return model
    matches = [k for k in PRICES if model.startswith(k)]
    return max(matches, key=len) if matches else None


def prices_for(model: str, on: date | None = None) -> tuple[float, float] | None:
    """(input, output) USD per 1M tokens for `model` on date `on` (default today).

    `on` defaults lazily (inside the call, not at import/module scope) so a
    long-running process that crosses the 2026-08-31 introductory cutoff
    mid-run reprices correctly instead of freezing "today" at process start.
    """
    key = price_key(model)
    if key is None:
        return None
    intro = _INTRO_PRICES.get(key)
    if intro is not None:
        rate, until = intro
        # date.today() is machine-local, not Anthropic's UTC billing day. In Sydney
        # (UTC+10/+11) local midnight on the cutover date lands ~10-11h before UTC
        # midnight, so the intro rate can be dropped locally before Anthropic's own
        # billing actually flips. That is the conservative direction (overstates
        # cost, never understates it), so it is left as-is rather than switched to
        # a UTC-anchored date.
        if (on or date.today()) <= until:
            return rate
    return PRICES[key]


def cost_usd(
    model: str,
    input_t: int,
    output_t: int,
    cache_write_t: int = 0,
    cache_read_t: int = 0,
    on: date | None = None,
    inference_geo: str | None = None,
) -> float:
    """Dollars for one call. Unknown model -> 0.0 (surfaced as an unpriced flag)."""
    price = prices_for(model, on)
    if price is None:
        return 0.0
    inp, out = price
    total = (
        input_t * inp
        + cache_write_t * inp * _CACHE_WRITE_MULT
        + cache_read_t * inp * _CACHE_READ_MULT_BY_KEY.get(price_key(model) or "", _CACHE_READ_MULT)
        + output_t * out
    ) / 1_000_000
    return total * _US_GEO_MULT if inference_geo == "us" else total


@dataclass
class UsageRecord:
    tool: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    unpriced: bool = False
    truncated: bool = False
    failed: bool = False
    provider: str = ""
    tenant_id: str = ""
    """The call raised. Anthropic still billed the input tokens it processed.

    Cost on a failed record is usually 0.0 and understates reality — the SDK
    raises before any usage object exists, so there is nothing to price. That is
    the point of the flag: the ledger says "this run made calls it cannot
    price", rather than silently reporting a total that is missing them.
    """


@dataclass
class CostLedger:
    deal_id: str = ""
    records: list[UsageRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _t_start: float = 0.0
    _t_stop: float = 0.0

    def record(self, rec: UsageRecord) -> None:
        with self._lock:
            self.records.append(rec)

    @property
    def total_usd(self) -> float:
        return sum(r.cost_usd for r in self.records)

    @property
    def calls(self) -> int:
        return len(self.records)

    @property
    def wall_clock_s(self) -> float:
        return (self._t_stop or time.monotonic()) - self._t_start if self._t_start else 0.0

    @property
    def sum_latency_s(self) -> float:
        return sum(r.latency_s for r in self.records)

    @property
    def has_unpriced(self) -> bool:
        """True if any call could not be priced, so `total_usd` understates spend.

        A boolean rather than a WARNING inside `summary()`: a caller that sets a
        price from this ledger has to be able to refuse, and it cannot grep a
        text blob for that.
        """
        return any(r.unpriced for r in self.records)

    @property
    def failed_calls(self) -> int:
        return sum(1 for r in self.records if r.failed)

    def by_tool(self) -> dict[str, dict]:
        agg: dict[str, dict] = {}
        for r in self.records:
            a = agg.setdefault(
                r.tool,
                {
                    "calls": 0,
                    "cost_usd": 0.0,
                    "input": 0,
                    "output": 0,
                    "cache_write": 0,
                    "cache_read": 0,
                    "latency_s": 0.0,
                    "max_output": 0,
                },
            )
            a["calls"] += 1
            a["cost_usd"] += r.cost_usd
            a["input"] += r.input_tokens
            a["output"] += r.output_tokens
            # Peak output sizes each call site's max_tokens cap (at least 4x observed peak).
            a["max_output"] = max(a["max_output"], r.output_tokens)
            a["cache_write"] += r.cache_write_tokens
            a["cache_read"] += r.cache_read_tokens
            a["latency_s"] += r.latency_s
        return agg

    def summary(self) -> str:
        lines = [
            f"Cost ledger — deal {self.deal_id or '(unscoped)'}: "
            f"${self.total_usd:.4f} over {self.calls} calls, "
            f"wall-clock {self.wall_clock_s:.0f}s (serial would be {self.sum_latency_s:.0f}s)"
        ]
        for tool, a in sorted(self.by_tool().items(), key=lambda kv: -kv[1]["cost_usd"]):
            lines.append(
                f"  {tool:<28} ${a['cost_usd']:.4f}  "
                f"{a['calls']} call(s)  in {a['input']:,} / out {a['output']:,} / "
                f"cache w {a['cache_write']:,} r {a['cache_read']:,}  "
                f"peak out {a['max_output']:,}  {a['latency_s']:.0f}s"
            )
        if self.failed_calls:
            failed = sorted({r.tool for r in self.records if r.failed})
            lines.append(
                f"  WARNING {self.failed_calls} FAILED call(s) — billed but unpriced: {', '.join(failed)}"
            )
        if any(r.truncated for r in self.records):
            tr = sorted({r.tool for r in self.records if r.truncated})
            # Not "(allowed)": the record is written before the raise, so a
            # truncation appears here whether it was tolerated via
            # allow_truncation or raised and failed the call. Either way the
            # actionable fact is the same — that cap is too low.
            lines.append(f"  WARNING TRUNCATED at max_tokens on: {', '.join(tr)} — raise the cap")
        if self.has_unpriced:
            unp = sorted({r.model for r in self.records if r.unpriced})
            lines.append(f"  WARNING unpriced model(s) — cost understated: {', '.join(unp)}")
        return "\n".join(lines)


# -- active-session plumbing ---------------------------------------------------
# A ContextVar, not a module global. The previous global had save/restore
# semantics, which is correct for one CLI script and wrong the moment this runs
# behind the API: two concurrent deals cross-attribute their spend into whichever
# session happened to be installed last, and the first one to exit restores `prev`
# while the second is still spending — so its remaining calls land in a ledger
# nobody reads. That is a billing error, not a race in a log.
#
# ContextVars do NOT propagate into ThreadPoolExecutor workers on their own, which
# would silently un-scope every parallel section, extractor and ingest call — the
# majority of the spend. `creditpaper.concurrency.pool` copies the context at
# submit, and a structural guard keeps bare executors out of the codebase.
_ACTIVE: ContextVar[CostLedger | None] = ContextVar("creditpaper_cost_ledger", default=None)

# Active organization context for durable provider attribution. Kept separate from
# CostLedger: API workers and ingestion can scope a tenant independently.
_ACTIVE_TENANT: ContextVar[str | None] = ContextVar("creditpaper_usage_tenant", default=None)

# S3-2: calls made with no session open are recorded nowhere and cost $0 in every
# report. Two real pipelines did exactly that. A counter plus a loud exit line is
# the minimum honest behaviour: the number is wrong AND you are told it is wrong.
_unscoped_total = 0
_unscoped_tools: set[str] = set()
_UNSCOPED_LOCK = threading.Lock()


def active_ledger() -> CostLedger | None:
    return _ACTIVE.get()


def active_tenant_id() -> str | None:
    return _ACTIVE_TENANT.get()


@contextmanager
def tenant_usage_session(tenant_id: str | None) -> Iterator[None]:
    """Attribute all nested LLM calls to one authenticated organization."""
    token = _ACTIVE_TENANT.set(str(tenant_id) if tenant_id else None)
    try:
        yield
    finally:
        _ACTIVE_TENANT.reset(token)


def note_unscoped_call(tool: str) -> None:
    """Record that a billable call happened outside any `cost_session`.

    Called from the transport when `active_ledger()` is None. Deliberately not a
    raise: refusing to run would turn a reporting gap into an outage, and some
    one-off scripts legitimately do not care about cost attribution. But it must
    never be silent, because the failure mode is a $0 report that reads as
    success.
    """
    global _unscoped_total
    with _UNSCOPED_LOCK:
        _unscoped_total += 1
        _unscoped_tools.add(tool)


def unscoped_call_count() -> int:
    return _unscoped_total


def unscoped_report() -> str | None:
    """A one-paragraph warning, or None if every call was attributed."""
    if not _unscoped_total:
        return None
    tools = ", ".join(sorted(_unscoped_tools))
    return (
        f"COST NOT RECORDED: {_unscoped_total} LLM call(s) ran outside any "
        f"cost_session and are missing from every cost report for this process "
        f"(tools: {tools}). Wrap the entry point in creditpaper.obs.ledger."
        f"cost_session(deal_id) so this spend is attributed."
    )


@atexit.register
def _warn_about_unscoped_calls() -> None:
    # stderr, not logging: this fires during interpreter shutdown, when logging
    # handlers may already be closed. An exception raised here is printed and
    # ignored by the interpreter, so raising would be theatre rather than a gate.
    report = unscoped_report()
    if report:
        print(os.linesep + report, file=sys.stderr)  # noqa: T201 — report output is the feature


@contextmanager
def cost_session(deal_id: str = "") -> Iterator[CostLedger]:
    """Scope a ledger to one deal, for this context and anything it spawns.

    Nesting is supported and restores the outer session on exit, but nesting is
    not how concurrency is handled: two deals running at once each get their own
    context, and neither can see or clobber the other's ledger.
    """
    led = CostLedger(deal_id=deal_id)
    led._t_start = time.monotonic()
    token = _ACTIVE.set(led)
    try:
        yield led
    finally:
        led._t_stop = time.monotonic()
        _ACTIVE.reset(token)
