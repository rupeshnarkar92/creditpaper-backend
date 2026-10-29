"""Track-1 numeric validation (D11): reconcile a deal model against curated ground
truth — "100% reconciled-or-explained". The signed paper is never read by an
extractor; its figures live in a hand-curated `ground_truth.json` and this harness
diffs every material number/value the model carries against them.

The comparison core (`reconcile`) is PURE and deterministic — no I/O, no API — so it
is exhaustively unit-tested and runs on any model (live-ingested or GT-populated) and
any deal. Tolerance is PRECISION-AWARE: a ground-truth figure is matched to the number
of decimal places it states (±1 in the last place absorbs rounding), so "44.93" matches
44.9266 but a $19 error on "7758697" does not. That keeps the bar tight enough to catch
real extraction errors while tolerating legitimate last-place rounding in derived values.

Ground-truth sections (see fixtures/.../ground_truth.json):
  figures_numeric   — anchored figures, Decimal-comparable (the core matches)
  derived_expected  — what model/derive.py must produce
  computed_ratios   — ratios stated in the paper AND recomputable (mapped to metrics.*)
  reference_values  — exact strings (entities, dates, addresses)
  explainable_differences — path -> reason; a mismatch there is EXPLAINED, not a failure
Keys beginning "_" (e.g. _note) are skipped; `pending`/`stated_not_modelled` are not
Track-1 matches.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

from creditpaper.model.citations import resolve_path
from creditpaper.model.core import Fact
from creditpaper.model.deal import DealModel

# computed_ratios keys -> the model path that should equal them (within rounding).
# Ratios the model deliberately does not carry are left unmapped (reported, not failed).
_RATIO_TO_PATH: dict[str, str] = {
    "lvr_net_gst_pct": "metrics.lvr_pct",
    "lcr_indicative_pct": "metrics.lcr_pct",
    "funding_table_total_stated": "economics.total_cost",
    "funding_table_senior_total_stated": "economics.senior_total",
}


class Verdict(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    MISSING = "missing"  # ground truth has it; the model does not
    EXPLAINED = "explained"  # mismatch covered by explainable_differences
    UNMODELLED = "unmodelled"  # a computed_ratio with no model path (informational)


@dataclass
class Line:
    path: str
    section: str
    expected: str
    actual: str | None
    verdict: Verdict
    note: str = ""


@dataclass
class ReconcileReport:
    deal: str = ""
    lines: list[Line] = field(default_factory=list)

    def _n(self, v: Verdict) -> int:
        return sum(1 for ln in self.lines if ln.verdict is v)

    @property
    def matched(self) -> int:
        return self._n(Verdict.MATCH)

    @property
    def mismatched(self) -> list[Line]:
        return [ln for ln in self.lines if ln.verdict is Verdict.MISMATCH]

    @property
    def missing(self) -> list[Line]:
        return [ln for ln in self.lines if ln.verdict is Verdict.MISSING]

    @property
    def explained(self) -> list[Line]:
        return [ln for ln in self.lines if ln.verdict is Verdict.EXPLAINED]

    @property
    def checked(self) -> int:
        """Lines that count toward the Track-1 bar (excludes informational unmodelled)."""
        return sum(1 for ln in self.lines if ln.verdict is not Verdict.UNMODELLED)

    @property
    def reconciled_or_explained(self) -> bool:
        """D11 Track-1 pass: every checked line is a match or an explained difference."""
        return all(ln.verdict in (Verdict.MATCH, Verdict.EXPLAINED, Verdict.UNMODELLED) for ln in self.lines)

    @property
    def pass_rate(self) -> float:
        ok = sum(1 for ln in self.lines if ln.verdict in (Verdict.MATCH, Verdict.EXPLAINED))
        return ok / self.checked if self.checked else 1.0

    def summary(self) -> str:
        head = (
            f"Track-1 {self.deal or '(deal)'}: "
            f"{'RECONCILED-OR-EXPLAINED' if self.reconciled_or_explained else 'DISCREPANCIES'} — "
            f"{self.matched}/{self.checked} matched, {len(self.mismatched)} mismatch, "
            f"{len(self.missing)} missing, {len(self.explained)} explained "
            f"({self.pass_rate * 100:.1f}% ok)"
        )
        shown = [
            ln for ln in self.lines if ln.verdict in (Verdict.MISMATCH, Verdict.MISSING, Verdict.EXPLAINED)
        ]
        rows = [
            f"  [{ln.verdict.value:9}] {ln.path}\n"
            f"        expected {ln.expected!r}  actual {ln.actual!r}" + (f"  ({ln.note})" if ln.note else "")
            for ln in shown
        ]
        return head + ("\n" + "\n".join(rows) if rows else "")


# -- ground truth loading ------------------------------------------------------


@dataclass
class GroundTruth:
    deal_name: str
    figures_numeric: dict[str, str]
    derived_expected: dict[str, str]
    computed_ratios: dict[str, str]
    reference_values: dict[str, str]
    explainable: dict[str, str]

    @staticmethod
    def _clean(section: object) -> dict[str, str]:
        """Drop comment keys ('_'-prefixed, e.g. _note) — every other key is a check."""
        if not isinstance(section, dict):
            return {}
        return {k: v for k, v in section.items() if not str(k).startswith("_")}

    @classmethod
    def load(cls, path: str | Path) -> GroundTruth:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            deal_name=data.get("deal_name", str(path)),
            figures_numeric=cls._clean(data.get("figures_numeric")),
            derived_expected=cls._clean(data.get("derived_expected")),
            computed_ratios=cls._clean(data.get("computed_ratios")),
            reference_values=cls._clean(data.get("reference_values")),
            explainable=cls._clean(data.get("explainable_differences")),
        )


# -- comparison primitives (pure) ----------------------------------------------


def _model_value(model: DealModel, path: str) -> Any:
    """The value at a model path. resolve_path yields a Fact (take .value) or a bare
    object; None if the path doesn't resolve."""
    target = resolve_path(model, path)
    return target.value if isinstance(target, Fact) else target


def _to_decimal(s: Any) -> Decimal | None:
    if isinstance(s, bool):  # bools are ints in Python; never a numeric figure
        return None
    try:
        return Decimal(str(s).replace(",", "").replace("$", "").replace("%", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _decimals(s: str) -> int:
    """Decimal places stated in a ground-truth number ('44.93' -> 2, '7758697' -> 0)."""
    txt = str(s).strip()
    return len(txt.split(".", 1)[1]) if "." in txt else 0


def numbers_match(expected: str, actual: Any) -> bool:
    """Precision-aware numeric match: equal at the ground truth's stated precision,
    tolerating ±1 in the last place (rounding). Non-numeric actual never matches."""
    exp, act = _to_decimal(expected), _to_decimal(actual)
    if exp is None or act is None:
        return False
    tol = Decimal(1).scaleb(-_decimals(expected))  # 0 dp -> 1, 2 dp -> 0.01
    return abs(act - exp) <= tol


def _norm_str(s: Any) -> str:
    """Normalise for string comparison: collapse whitespace, strip surrounding quotes
    and trailing punctuation, casefold."""
    return " ".join(str(s).split()).strip().strip("\"'").rstrip(".,;").casefold()


def strings_match(expected: str, actual: Any) -> bool:
    if actual is None:
        return False
    e, a = _norm_str(expected), _norm_str(actual)
    if not e:
        return not a
    if e == a or e in a or a in e:  # exact / one is a substring of the other
        return True
    # Token-subset: tolerate inserted tokens (e.g. "Riverton 2026" vs "Riverton
    # NSW 2026") — one value's words are all present in the other. Reference values are
    # entities/addresses, where a fuller restatement is the same fact, not a mismatch.
    te, ta = _tokens(e), _tokens(a)
    return bool(te) and bool(ta) and (te <= ta or ta <= te)


# Title-reference long forms, so "Lot 7 in Deposited Plan 123456" reads as "Lot 7 DP 123456".
_TITLE_FORMS = (
    (re.compile(r"\bdeposited plan\b"), "dp"),
    (re.compile(r"\bstrata plan\b"), "sp"),
    (re.compile(r"\blot (\w+) in\b"), r"lot \1"),
)


def _tokens(s: str) -> set[str]:
    """Word tokens for the subset test: punctuation is not part of a word
    ("Riverton," is "riverton"), and a title reference's long form is its short form."""
    for rx, repl in _TITLE_FORMS:
        s = rx.sub(repl, s)
    return set(re.findall(r"[^\W_]+", s))


# -- the reconciliation core ---------------------------------------------------


def _verdict(path: str, expected: str, actual: Any, *, numeric: bool, explainable: dict[str, str]) -> Line:
    if actual is None:
        v = Verdict.MISSING
    elif numbers_match(expected, actual) if numeric else strings_match(expected, actual):
        v = Verdict.MATCH
    else:
        v = Verdict.MISMATCH
    note = ""
    if v in (Verdict.MISMATCH, Verdict.MISSING) and path in explainable:
        v, note = Verdict.EXPLAINED, explainable[path]
    return Line(
        path=path,
        section="",
        expected=str(expected),
        actual=(None if actual is None else str(actual)),
        verdict=v,
        note=note,
    )


def reconcile(model: DealModel, gt: GroundTruth) -> ReconcileReport:
    """Diff every ground-truth value against the model. Pure — no I/O, no network."""
    rep = ReconcileReport(deal=gt.deal_name)

    for section, items, numeric in (
        ("figures_numeric", gt.figures_numeric, True),
        ("derived_expected", gt.derived_expected, True),
        ("reference_values", gt.reference_values, False),
    ):
        for path, expected in items.items():
            ln = _verdict(
                path, expected, _model_value(model, path), numeric=numeric, explainable=gt.explainable
            )
            ln.section = section
            rep.lines.append(ln)

    # computed_ratios: only those mapped to a model path are checked; the rest are
    # informational (the model deliberately doesn't carry investor-return ratios).
    for key, expected in gt.computed_ratios.items():
        path = _RATIO_TO_PATH.get(key)
        if path is None:
            rep.lines.append(
                Line(
                    path=f"computed_ratios.{key}",
                    section="computed_ratios",
                    expected=str(expected),
                    actual=None,
                    verdict=Verdict.UNMODELLED,
                    note="not carried by the model",
                )
            )
            continue
        ln = _verdict(path, expected, _model_value(model, path), numeric=True, explainable=gt.explainable)
        ln.section = "computed_ratios"
        rep.lines.append(ln)

    return rep
