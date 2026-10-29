"""How numbers are written in the paper — one place, one rounding mode.

Values are stored at full extracted precision and are NEVER rounded in storage;
rounding happens here, at the presentation boundary, and only here
(`DECISIONS.md` 2026-08-21).

Two things this fixes.

**One rounding mode.** The renderer formatted with `f"{value:,.0f}"`, which uses
Decimal's default `ROUND_HALF_EVEN` (banker's rounding), while `model/derive.py`
quantises with `ROUND_HALF_UP`. So a derived total and the column of line items
above it could each be rounded a different way, and a funding table could print a
column that does not add up to its own printed total — in a document whose entire
claim is that its numbers reconcile. `ROUND_HALF_UP` everywhere: it is the
convention finance uses, and matching `derive` is what makes the arithmetic agree.

**One formatter.** The `:.2f% of {basis}` construction existed twice, in the
snapshot and in the funding table, describing the same ratio. Two copies of a
format string are two things that can drift, and this codebase has already been
bitten by exactly that with LVR (rendered three different ways across Sections 2,
6 and 8 before `DerivedMetrics` existed).

Full precision is preserved on the provenance and audit surfaces deliberately.
The cents are diagnostic: a QS figure ending `.81` is what identified the
cost-to-complete basis in the `qs_vs_funding` investigation, and rounding it away
in the audit trail would have hidden the evidence.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# Credit-paper convention: whole dollars with thousands separators. Cents are
# noise in a document about millions — but see the module docstring: they survive
# in the model and on the audit surfaces.
_MONEY_Q = Decimal("1")
_PCT_Q = Decimal("0.01")

EM_DASH = "—"  # what a missing value looks like in a table


def money(value: Decimal | int | None, *, dash: str = EM_DASH) -> str:
    """`$1,234,568`. Whole dollars, thousands separators, ROUND_HALF_UP."""
    if value is None:
        return dash
    return f"${Decimal(value).quantize(_MONEY_Q, rounding=ROUND_HALF_UP):,}"


def percent(value: Decimal | int | None, *, dash: str = EM_DASH) -> str:
    """`44.93%`. Two decimal places, ROUND_HALF_UP."""
    if value is None:
        return dash
    return f"{Decimal(value).quantize(_PCT_Q, rounding=ROUND_HALF_UP)}%"


def percent_of(value: Decimal | int | None, basis: str, *, dash: str = EM_DASH) -> str:
    """`44.93% of As If Complete (ex GST)` — a ratio always names its basis."""
    if value is None:
        return dash
    return f"{percent(value)} of {basis}"


def exact(value: Decimal | int | None, *, dash: str = EM_DASH) -> str:
    """The value as extracted, unrounded — for provenance and audit surfaces.

    Deliberately separate from `money`: the reader of a funding table wants whole
    dollars, and the reader of an audit trail wants the cents that prove which
    column a figure came from.
    """
    if value is None:
        return dash
    return f"{Decimal(value):,}"
