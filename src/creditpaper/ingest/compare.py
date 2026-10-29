"""Same-concept comparison: five-way classification of a disagreement (spec 3a).

Two assertions of one concept are compared only when their qualifiers are
compatible, and every difference gets one class. None is swallowed:

- ``SUPERSEDED``: one side comes from a superseded document version (a draft against
  the executed instrument). The draft is excluded; the difference is noted.
- ``TEMPORAL``: the two speak at different times (non-overlapping validity periods,
  or different as-at dates). Both are kept on the timeline.
- ``BASIS_DIFFERENCE``: incompatible qualifiers (GST incl. vs excl., valuation basis,
  currency, period). Both are kept. For GST the incl./excl. relationship must hold
  at the statutory rate, or the pair is a ``CONTRADICTION``.
- ``AGREE`` / ``ROUNDING``: equal, or consistent within the stated precision
  ("$12.4m" against 12,437,000).
- ``CONTRADICTION``: same concept, compatible qualifiers, values differ beyond the
  comparison rule. Held; an analyst resolves it with a logged reason (D5).

The order is fixed and is the order above. Pure functions: no database access.

Stated precision is read from the value itself: a ``Fact`` keeps the figure, not the
written form, so "$12.4m" arrives as 12400000 and its precision is the unit of its
last significant digit (100,000). That reading cannot tell "$12m" from a precise
12,000,000, so a rounded statement counts as ROUNDING only when its precision is
fine relative to its size (``MAX_ROUNDING_STEP``). A coarser pair reads as a
contradiction, which is held for a human: the safe direction.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Final

from creditpaper.ingest.values import GstBasis
from creditpaper.model.concepts import Compare, Concept, Qualifier, normalise_label
from creditpaper.model.core import Fact, FactBasis

__all__ = [
    "GST_RATE",
    "MAX_ROUNDING_STEP",
    "MAX_STATED_PRECISION_STEP",
    "DiffClass",
    "classify",
    "source_key",
]

# Australian GST, A New Tax System (Goods and Services Tax) Act 1999 s 9-70: 10%.
GST_RATE: Final = Decimal("0.10")

# A rounded money or count figure (Compare.EXACT) is consistent with a precise one
# only when its precision step is at most 1% of its size: "$12.4m" (step 100,000)
# qualifies, "$12m" (step 1,000,000, 8%) does not.
MAX_ROUNDING_STEP: Final = Decimal("0.01")
# Figures the registry marks as read within their stated precision (areas): 10%.
MAX_STATED_PRECISION_STEP: Final = Decimal("0.10")

# Money is compared to the dollar: the GST arithmetic tolerates cent rounding.
_DOLLAR: Final = Decimal("1")
_PCT: Final = Decimal("0.01")


class DiffClass(str, Enum):
    AGREE = "agree"
    CONTRADICTION = "contradiction"
    BASIS_DIFFERENCE = "basis_difference"
    ROUNDING = "rounding"
    SUPERSEDED = "superseded"
    TEMPORAL = "temporal"
    # A disagreement where only ONE side comes from a document filed in a slot the
    # concept names as a source (DECISIONS 2026-10-09): noted, never blocking.
    OUT_OF_AUTHORITY = "out_of_authority"


def source_key(fact: Fact[Any]) -> str | None:
    """The document version a fact came from (the document when no version is recorded)."""
    prov = fact.provenance
    if prov is None:
        return None
    return prov.document_version_id or prov.document_id


def classify(concept: Concept, a: Fact[Any], b: Fact[Any], *, superseded: Callable[[str], bool]) -> DiffClass:
    """Classify two assertions of ``concept``. ``superseded(key)`` answers whether a
    document version (``source_key``) has been superseded by a later version."""
    if a.value is None or b.value is None:
        raise ValueError("classify compares two stated values")
    for fact in (a, b):
        key = source_key(fact)
        if key is not None and superseded(key):
            return DiffClass.SUPERSEDED
    if _temporal(concept, a, b):
        return DiffClass.TEMPORAL
    basis = _basis(concept, a.basis, b.basis)
    if basis is not None:
        return basis if basis is not _GST else _gst_relationship(concept, a, b)
    rule = _rule(concept, a.value, b.value)
    return rule if rule is not None else DiffClass.CONTRADICTION


# --- 2. temporal --------------------------------------------------------------------


def _temporal(concept: Concept, a: Fact[Any], b: Fact[Any]) -> bool:
    if a.valid_to is not None and b.valid_from is not None and a.valid_to <= b.valid_from:
        return True
    if b.valid_to is not None and a.valid_from is not None and b.valid_to <= a.valid_from:
        return True
    if Qualifier.AS_AT in concept.qualifiers and a.basis and b.basis:
        return a.basis.as_at is not None and b.basis.as_at is not None and a.basis.as_at != b.basis.as_at
    return False


# --- 3. basis -----------------------------------------------------------------------

_GST = object()  # sentinel: the only incompatibility is incl. vs excl. GST
_STATED_GST = frozenset({GstBasis.INCLUSIVE, GstBasis.EXCLUSIVE})


def _same_text(x: str | None, y: str | None) -> bool:
    return x is None or y is None or x.strip().casefold() == y.strip().casefold()


def _basis(concept: Concept, a: FactBasis | None, b: FactBasis | None) -> Any:
    """BASIS_DIFFERENCE, the GST sentinel, or None when the qualifiers are compatible.

    Only the concept's own qualifiers count. An unrecorded qualifier (None), an
    UNSTATED or a MIXED GST basis is compatible with anything: the values are then
    compared directly, and a difference is a contradiction for a human.
    """
    if a is None or b is None:
        return None
    wanted = set(concept.qualifiers)
    if Qualifier.VALUATION_BASIS in wanted and not _same_text(a.valuation_basis, b.valuation_basis):
        return DiffClass.BASIS_DIFFERENCE
    if Qualifier.CURRENCY in wanted and not _same_text(a.currency, b.currency):
        return DiffClass.BASIS_DIFFERENCE
    if Qualifier.PERIOD in wanted and not _same_text(a.period, b.period):
        return DiffClass.BASIS_DIFFERENCE
    if Qualifier.GST_BASIS in wanted and a.gst in _STATED_GST and b.gst in _STATED_GST and a.gst is not b.gst:
        return _GST
    return None


def _gst_relationship(concept: Concept, a: Fact[Any], b: Fact[Any]) -> DiffClass:
    """incl. = excl. x (1 + GST_RATE), to the dollar or within the stated precision."""
    incl, excl = (a, b) if a.basis and a.basis.gst is GstBasis.INCLUSIVE else (b, a)
    stated, base = _decimal(incl.value), _decimal(excl.value)
    if stated is None or base is None:
        return DiffClass.CONTRADICTION
    implied = base * (1 + GST_RATE)
    if abs(implied - stated) <= _DOLLAR or _rounding(concept, implied, stated) is not None:
        return DiffClass.BASIS_DIFFERENCE
    return DiffClass.CONTRADICTION


# --- 4. the comparison rule ---------------------------------------------------------


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _step(d: Decimal) -> Decimal:
    """The unit of ``d``'s last significant digit: 12400000 -> 100000, 8.5 -> 0.1."""
    exponent = d.normalize().as_tuple().exponent
    return Decimal(1).scaleb(exponent if isinstance(exponent, int) else 0)


def _rounding(concept: Concept, x: Decimal, y: Decimal) -> DiffClass | None:
    """ROUNDING when the more precise value rounds to the coarser one at its precision."""
    if x.is_zero() or y.is_zero():
        return None
    coarse, fine = (x, y) if _step(x) > _step(y) else (y, x)
    step = _step(coarse)
    if step == _step(fine):
        return None
    limit = MAX_STATED_PRECISION_STEP if concept.compare is Compare.STATED_PRECISION else MAX_ROUNDING_STEP
    if step / abs(coarse) > limit:
        return None
    return DiffClass.ROUNDING if fine.quantize(step, rounding=ROUND_HALF_UP) == coarse else None


def _date(value: Any) -> datetime.date | None:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _rule(concept: Concept, x: Any, y: Any) -> DiffClass | None:
    """AGREE or ROUNDING under the concept's comparison rule, else None."""
    if concept.compare is Compare.TEXT_NORMALISED:
        return DiffClass.AGREE if normalise_label(str(x)) == normalise_label(str(y)) else None
    if concept.compare is Compare.DATE:
        day_x, day_y = _date(x), _date(y)
        return DiffClass.AGREE if day_x is not None and day_x == day_y else None
    dx, dy = _decimal(x), _decimal(y)
    if dx is None or dy is None:
        return DiffClass.AGREE if x == y else None
    if dx == dy:
        return DiffClass.AGREE
    if concept.compare is Compare.PCT_2DP:
        same = dx.quantize(_PCT, rounding=ROUND_HALF_UP) == dy.quantize(_PCT, rounding=ROUND_HALF_UP)
        return DiffClass.ROUNDING if same else None
    return _rounding(concept, dx, dy)
