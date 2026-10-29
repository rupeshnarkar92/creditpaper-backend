"""Comparable sales tables, read by code: every row, roles proven by arithmetic.

Measured 2026-10-08 on blind_01: most valuation figures the signed credit paper
cites that ingestion missed were comparable sales. The fields exist
(`valuation.comp_set[*]`), but the model mentions a few rows of a table and the
rest are lost, and which few changes between reads. Reading the table in code
gets every row, the same way every time, at no model cost.

A row is read only when its roles can be told apart, and PROVEN only when the
arithmetic of a comparable holds: area x rate = price (within 3%, the rounding
a valuer applies to a rate). A proven row passes at the verified confidence; an
unproven one (the page is a comparable table but this row's figures do not
multiply out) is still read, at a confidence below the review threshold, so an
analyst sees it. Nothing is invented: a month-only sale date ("Mar-25") stays
unset rather than becoming the 1st, and a row with no label gets a positional
key that is stable for the same page.

A page is a comparable table only when it says so ("comparable", "sales
evidence") and holds three or more rows with a sale price; prose with figures,
and other tables (a funding table), produce nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from creditpaper.ingest.values import Figure, figures
from creditpaper.model.citations import slugify

__all__ = ["CompRow", "read_comparables"]

_COMPARABLE_WORDING = re.compile(r"\bcomparable|\bsales?\s+evidence\b", re.IGNORECASE)
_LAND_WORDING = re.compile(r"\b(site|land)\s+(sales?|area)\b|\bland\s+sales?\b", re.IGNORECASE)
_MIN_ROWS = 3
_MIN_PRICE = Decimal(50_000)
_RATE_RANGE = (Decimal(50), Decimal(100_000))
_AREA_RANGE = (Decimal(10), Decimal(500_000))
_PROOF_TOLERANCE = Decimal("0.03")


@dataclass(frozen=True)
class CompRow:
    key: str
    line: str  # the row as it stands on the page: the clause the facts cite
    start: int  # character offsets of the row in the page text
    end: int
    sale_price: Decimal
    derived_rate: Decimal | None
    area: Decimal | None
    area_field: str  # "land_area" for site sales, else "gfa_nla"
    sale_date: date | None
    proven: bool


def _decimal(fig: Figure) -> Decimal | None:
    try:
        return Decimal(fig.value)
    except (InvalidOperation, ValueError):
        return None


def _full_date(figs: list[Figure]) -> date | None:
    dates = [f.value for f in figs if f.kind.value == "date" and len(f.value) == 10]
    if len(dates) != 1:
        return None
    try:
        return date.fromisoformat(dates[0])
    except ValueError:
        return None


_PERIOD = re.compile(r"\bQ[1-4]\s*(?:19|20)\d\d\b|\b[A-Z][a-z]{2}-\d{2}\b")
# Short tokens that sit among a row's figures without being a label ("3 + S", "BR").
_CELL_TOKEN = re.compile(r"^(?:[+&/x-]|[A-Za-z]{1,2}|\(?[A-Za-z]{1,3}\)?)$")


def _label_end(line: str, figs: list[Figure], price_fig: Figure) -> int:
    """Where the row's label stops: before the run of cells (figures and short tokens) ending at the price."""
    cut = price_fig.start
    for fig in reversed([f for f in figs if f.end <= price_fig.start]):
        between = line[fig.end : cut].split()
        if all(_CELL_TOKEN.match(tok) for tok in between):
            cut = fig.start
        else:
            break
    return cut


def _row(text: str, start: int, end: int) -> tuple[str, CompRow] | None:
    line = text[start:end]
    figs = figures(line)
    money = [(f, _decimal(f)) for f in figs if f.kind.value == "money"]
    prices = [(f, v) for f, v in money if v is not None and v >= _MIN_PRICE]
    if len(prices) != 1:
        return None
    price_fig, price = prices[0]
    assert price is not None
    after = [f for f in figs if f.start > price_fig.start and f.kind.value in ("money", "number")]
    rate = next(
        (
            v
            for f in after
            if (v := _decimal(f)) is not None and _RATE_RANGE[0] <= v <= _RATE_RANGE[1] and v < price
        ),
        None,
    )
    # A comparable is a SALE: it carries a date. A row without one is the subject's
    # own schedule (assessed values per unit), which is not comparable evidence.
    if not (any(f.kind.value == "date" for f in figs) or _PERIOD.search(line)):
        return None
    before = [
        (f, v)
        for f in figs
        if f.end <= price_fig.start
        and f.kind.value == "number"
        and (v := _decimal(f)) is not None
        and _AREA_RANGE[0] <= v <= _AREA_RANGE[1]
    ]
    # The area is the number the arithmetic proves (area x rate = price), not the
    # nearest one: a row may hold bedrooms or an outdoor area beside it.
    proving = [
        (f, v) for f, v in before if rate is not None and abs(v * rate - price) <= price * _PROOF_TOLERANCE
    ]
    area_fig, area = proving[-1] if proving else (before[-1] if before else (None, None))
    proven = bool(proving)
    label = re.sub(r"\s+", " ", line[: _label_end(line, figs, price_fig)]).strip(" ,;:-|")
    row = CompRow(
        key="",
        line=line.strip(),
        start=start,
        end=end,
        sale_price=price,
        derived_rate=rate,
        area=area,
        area_field="gfa_nla",
        sale_date=_full_date(figs),
        proven=proven,
    )
    return label, row


def read_comparables(page_text: str) -> list[CompRow]:
    """Every row of the comparable sales table(s) on one page; empty when the page holds none."""
    if not _COMPARABLE_WORDING.search(page_text):
        return []
    area_field = "land_area" if _LAND_WORDING.search(page_text) else "gfa_nla"
    found: list[tuple[str, CompRow]] = []
    offset = 0
    for raw in page_text.splitlines(keepends=True):
        start, end = offset, offset + len(raw.rstrip("\r\n"))
        offset += len(raw)
        parsed = _row(page_text, start, end)
        if parsed is not None:
            found.append(parsed)
    if len(found) < _MIN_ROWS:
        return []
    rows: list[CompRow] = []
    seen: set[str] = set()
    for n, (label, row) in enumerate(found, start=1):
        key = slugify(label) if re.search(r"[A-Za-z]", label) else ""
        if not key or key in seen:
            key = f"row_{n}"
        seen.add(key)
        rows.append(
            CompRow(
                key=key,
                line=row.line,
                start=row.start,
                end=row.end,
                sale_price=row.sale_price,
                derived_rate=row.derived_rate,
                area=row.area,
                area_field=area_field,
                sale_date=row.sale_date,
                proven=row.proven,
            )
        )
    return rows
