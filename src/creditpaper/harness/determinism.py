"""Run-to-run determinism measurement — a DIFFERENT axis from Track-1 accuracy.

`reconcile.py` asks "does the model agree with curated ground truth?" (D11 Track 1).
This module asks "does the model agree WITH ITSELF across repeated runs over the same
document?" Two live ingestion runs on the same deal produced 384 facts and then 370
facts, and the cross-document `qs_vs_funding` check flagged four divergences on one
run and one on the next — three "catches" that appeared, then vanished, with no way
to tell whether the first run false-positived or the second false-negatived. Without
sampling knobs (Opus 5 / Sonnet 5 accept no temperature/top_p/top_k), the only lever
left is to MEASURE the variance precisely enough to localise it to specific field
paths, rather than argue about it deal by deal.

The comparison core (`measure_determinism`) is PURE and I/O-free, matching
reconcile.py's split between a testable core and a thin runner (scripts/
measure_determinism.py runs the extractor N times and hands the results here). It
takes N runs, each a mapping of citation-path -> Fact exactly as ExtractionOutput.fields
carries it, and buckets every path seen across any run into one of:

  stable            — present in every run, with an equal value and an equal anchor.
  flapping          — present in SOME runs and absent in others (the completeness
                       failure; `_doc.add_fact`'s now-flagged silent drops are the
                       likely mechanism, so a flapping path and a "no value returned
                       by the model" flag on the same path corroborate each other).
  value_divergent   — present in every run, but with genuinely differing values (the
                       most dangerous case: the fact LOOKS confidently present in
                       every run, so nothing about its presence signals the risk).
  anchor_divergent  — same value everywhere, but the page/clause it was anchored to
                       moved between runs. Lower severity than value-divergent (the
                       fact is right) but not nothing: D4 makes the anchor the thing
                       that lets a human verify the fact, and a wandering anchor means
                       that verification is itself non-reproducible.

Value comparison deliberately does NOT reuse reconcile.py's ±1-in-the-last-place
tolerance (see `_values_equal` for the full reasoning): it normalises REPRESENTATION
only — "6250000" vs "6250000.00" is the same fact, not a divergence — and then
compares exactly. A genuine numeric difference, even by 1 in the last digit of a
dollar figure, is reported as value-divergent. Track-1 tolerates rounding because it
compares against a human-transcribed ground truth that itself rounds; this harness
compares one run against another reading the SAME document with the SAME code, where
there is no presentation gap to absorb and a tolerance would hide the very variance
it exists to measure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from creditpaper.model.core import Fact


@dataclass
class FlappingPath:
    path: str
    present_count: int
    total_runs: int

    def describe(self) -> str:
        return f"{self.present_count} of {self.total_runs} runs"


@dataclass
class ValueDivergence:
    path: str
    # str(value) -> how many runs produced a value in that equivalence class.
    # A dict, not a Counter, so it round-trips through dataclass equality in tests.
    values: dict[str, int] = field(default_factory=dict)


@dataclass
class AnchorDivergence:
    path: str
    value: Any
    # "page N: <clause>" -> how many runs anchored the (agreed) value there.
    anchors: dict[str, int] = field(default_factory=dict)


@dataclass
class DeterminismReport:
    n_runs: int
    total_paths: int  # distinct citation paths seen across ANY run
    stable: list[str] = field(default_factory=list)
    flapping: list[FlappingPath] = field(default_factory=list)
    value_divergent: list[ValueDivergence] = field(default_factory=list)
    anchor_divergent: list[AnchorDivergence] = field(default_factory=list)
    # Set only when the report can't say anything meaningful (n_runs < 2) — the
    # caller must not read `stable`/`stability_rate` as a real result in that case.
    note: str = ""

    @property
    def stability_rate(self) -> float:
        """stable ÷ total distinct paths. 1.0 on zero paths (vacuously stable), but
        NEVER read this as "100% stable" when `note` is set — with fewer than two
        runs there is nothing to compare, so `stable` is deliberately left empty
        rather than defaulting every path to "stable" by not being checked."""
        return len(self.stable) / self.total_paths if self.total_paths else 1.0

    def summary_line(self) -> str:
        if self.note:
            return f"determinism: {self.note}"
        return (
            f"determinism: {self.n_runs} runs, {self.total_paths} distinct paths — "
            f"{len(self.stable)} stable, {len(self.flapping)} flapping, "
            f"{len(self.value_divergent)} value-divergent, "
            f"{len(self.anchor_divergent)} anchor-divergent "
            f"({self.stability_rate * 100:.1f}% stable)"
        )


def _to_decimal(v: Any) -> Decimal | None:
    """Strip a Fact value's textual formatting (commas, $, %) so representation
    doesn't get in the way of the exact comparison in `_values_equal`. Deliberately
    NOT `reconcile._to_decimal`: that helper is private to reconcile.py and, more to
    the point, its only caller (`numbers_match`) is exactly the rounding-tolerant
    comparison this module must not use — importing it would invite exactly that
    coupling back in."""
    if isinstance(v, bool):
        return None
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _norm_str(s: Any) -> str:
    """Collapse whitespace and casefold — REPRESENTATION normalisation only.
    Deliberately narrower than reconcile.strings_match, which also treats one
    string being a substring/token-subset of the other as a match: that is
    reconcile's OWN tolerance (appropriate against a human-transcribed ground
    truth, which may restate a fact more fully) and wrong here for the same
    reason the numeric tolerance is wrong below — two runs over the identical
    document are not entitled to disagree on wording."""
    return " ".join(str(s).split()).casefold()


def _values_equal(a: Any, b: Any) -> bool:
    """True if `a` and `b` are the SAME value once representation is normalised —
    and this is where this harness deliberately parts ways with reconcile.py's
    numbers_match/strings_match. It is a considered choice, not an oversight.

    reconcile.py (Track 1) compares an extraction against a curated ground truth a
    human transcribed off a paper that itself rounds: "44.93%" matching a computed
    44.9266% is a presentation difference there, and reconcile's ±1-in-the-last-
    place tolerance correctly absorbs it. This harness compares run A against run B
    reading the IDENTICAL document with the IDENTICAL code — there is no
    presentation gap to absorb. If one run reads 6,172,027 and the next reads
    6,172,028, that is not rounding; it is the model reading the same figure two
    different ways, which is precisely the variance this instrument exists to
    expose. Tolerating it would hide the exact class of finding this project keeps
    a permanent regression test for (test_crosscheck_catches_the_original_19_
    dollar_error, tests/test_derive.py) — a single-digit-off dollar figure in a
    funding table is not noise to a credit committee, it is the thing that ends a
    deal. So: normalise REPRESENTATION (scale/precision of the same number,
    whitespace/case of the same string) but never tolerate a genuine difference,
    however small. Pinned by
    test_single_dollar_digit_difference_is_value_divergent_not_stable — if that
    test ever needs to change to pass, the fix belongs in the test's expectation,
    not in reintroducing tolerance here because it "looks like reconcile.py".
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    an, bn = _to_decimal(a), _to_decimal(b)
    if an is not None and bn is not None:
        return an == bn
    return _norm_str(a) == _norm_str(b)


def _group_values(values: list[Any]) -> list[tuple[Any, int]]:
    """Bucket a path's per-run values into equivalence classes under `_values_equal`,
    preserving first-seen order (so output is deterministic run to run)."""
    groups: list[tuple[Any, int]] = []
    for v in values:
        for i, (rep, count) in enumerate(groups):
            if _values_equal(rep, v):
                groups[i] = (rep, count + 1)
                break
        else:
            groups.append((v, 1))
    return groups


def _anchor_key(fact: Fact) -> str:
    """Provenance identity for anchor-drift detection. Missing provenance on an
    EXTRACTED/CONFIRMED fact can't happen (Fact._no_anchor_no_fact enforces D4 at
    construction), but reads as its own bucket rather than raising, so a determinism
    run over unexpected input degrades to a reported anomaly, not a crash."""
    prov = fact.provenance
    if prov is None:
        return "(no provenance)"
    page = prov.page if prov.page is not None else "?"
    clause = (prov.location or "").strip()
    return f"page {page}: {clause}"


def measure_determinism(runs: list[dict[str, Fact]]) -> DeterminismReport:
    """Compare N runs of extracted facts (each keyed by citation path, as
    ExtractionOutput.fields carries them) and bucket every path seen. Pure — no I/O,
    no network, no API key required; the script half (scripts/measure_determinism.py)
    supplies the runs by actually calling an extractor N times."""
    n = len(runs)
    all_paths = sorted({p for run in runs for p in run})

    if n < 2:
        # "Handled sanely" means: don't crash, and don't claim perfect stability
        # from a single observation — there is nothing to compare it against.
        return DeterminismReport(
            n_runs=n,
            total_paths=len(all_paths),
            note=(
                f"only {n} run supplied — determinism cannot be measured from a "
                "single observation; run the extractor at least twice"
            ),
        )

    stable: list[str] = []
    flapping: list[FlappingPath] = []
    value_divergent: list[ValueDivergence] = []
    anchor_divergent: list[AnchorDivergence] = []

    for path in all_paths:
        # A Fact with no value never legitimately reaches ExtractionOutput.fields
        # (add_fact drops it before construction), but guard anyway — treating a
        # None-valued Fact as "absent" rather than crashing on the comparison below.
        present = [run[path] for run in runs if path in run and run[path].value is not None]
        if len(present) < n:
            flapping.append(FlappingPath(path=path, present_count=len(present), total_runs=n))
            continue

        values = [f.value for f in present]
        groups = _group_values(values)
        if len(groups) > 1:
            value_divergent.append(ValueDivergence(path=path, values={str(v): c for v, c in groups}))
            continue

        anchor_groups = _group_values_by_key(present, key=_anchor_key)
        if len(anchor_groups) > 1:
            anchor_divergent.append(
                AnchorDivergence(path=path, value=groups[0][0], anchors=dict(anchor_groups))
            )
        else:
            stable.append(path)

    return DeterminismReport(
        n_runs=n,
        total_paths=len(all_paths),
        stable=stable,
        flapping=flapping,
        value_divergent=value_divergent,
        anchor_divergent=anchor_divergent,
    )


def _group_values_by_key(facts: list[Fact], *, key) -> list[tuple[str, int]]:
    """Frequency of each distinct `key(fact)`, first-seen order — the anchor
    analogue of `_group_values` (anchors compare by exact string, not `_values_equal`;
    a moved page or reworded clause IS the signal, not noise to smooth over)."""
    counts: dict[str, int] = {}
    for f in facts:
        k = key(f)
        counts[k] = counts.get(k, 0) + 1
    return list(counts.items())
