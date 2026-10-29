"""Turn the determinism measurement into a bar the harness holds.

`harness/determinism.py` already classifies citation paths across N runs into
stable, flapping, value-divergent and anchor-divergent. It exists because two
live runs on one deal produced 384 facts and then 370, and a reconciliation
reported four divergences on one run and one on the next.

This module makes that a number with a threshold rather than a report someone
reads. A flagged fact is only worth flagging if it is there next time; a catch
that appears and vanishes teaches an analyst to ignore every catch, which costs
more than the catch was ever worth.

Three things count against stability, not for it:

  * A flapping path - present in some runs, absent in others.
  * A value divergence - present every time with a different answer. Worse than
    absence, because it looks settled.
  * An anchor divergence - the same value cited to a different place. The figure
    survives; the citation the paper prints does not.

And a report that could not measure is not a pass. `DeterminismReport` sets
`note` when it had fewer than two runs, and warns in its own docstring that
`stability_rate` must not be read as a result in that case.
"""

from __future__ import annotations

from pydantic import BaseModel

from creditpaper.harness.determinism import DeterminismReport

# The share of citation paths that must be identical across runs. Set at 0.98
# rather than 1.0 because a model is not a pure function and one path in fifty
# moving is tolerable; below that the findings stop being trustworthy.
STABILITY_BAR = 0.98


class Stability(BaseModel):
    runs: int
    paths: int
    stable: int
    flapping: tuple[str, ...] = ()
    diverging: tuple[str, ...] = ()
    note: str = ""

    @property
    def share(self) -> float:
        # Zero over zero is not a perfect score: a run that produced nothing
        # proves nothing, and must not read as 100% stable.
        if self.paths <= 0:
            return 0.0
        return self.stable / self.paths

    @property
    def passes(self) -> bool:
        if self.note:
            # The measurement could not be made. Unproven is not passed.
            return False
        return self.share >= STABILITY_BAR and not self.diverging


def stability_from(report: DeterminismReport) -> Stability:
    flapping = tuple(sorted(p.path for p in report.flapping))
    diverging = tuple(
        sorted([d.path for d in report.value_divergent] + [a.path for a in report.anchor_divergent])
    )
    return Stability(
        runs=report.n_runs,
        paths=report.total_paths,
        stable=len(report.stable),
        flapping=flapping,
        diverging=diverging,
        note=report.note,
    )


def assert_stable(stability: Stability) -> None:
    """Raise with the paths named, so the failure says what to go and look at."""
    if stability.passes:
        return
    if stability.note:
        raise AssertionError(f"determinism not measured: {stability.note}")
    detail = [f"stability {stability.share:.1%} over {stability.runs} runs (bar {STABILITY_BAR:.0%})"]
    if stability.flapping:
        detail.append("flapping: " + ", ".join(stability.flapping))
    if stability.diverging:
        detail.append("diverging: " + ", ".join(stability.diverging))
    raise AssertionError("; ".join(detail))


# --- the bar the corpus path is actually held to -------------------------------------
#
# `STABILITY_BAR` scores one number over EVERY path either read emitted, and on
# a general-extraction corpus that number cannot be met and does not mean much.
# Measured 2026-09-24 across three deal folders: 372 of 404 distinct paths are
# collection rows - individual comparable sales, macro-factor bullets - so
# reading more finds more of them and the denominator grows with recall. The
# share fell when recall improved, which is the wrong direction for a quality
# bar to move.
#
# This is the bar that says what the product promises, and it is narrower and
# harder in the place that counts:
#
#     NO MATERIAL SCALAR FACT MAY GO MISSING BETWEEN TWO READS OF THE SAME
#     DOCUMENTS. Disagreement about one is allowed and must SURFACE.
#
# Material: the concept registry's own flag, the figures a credit paper is
# built from. Scalar: not a collection row, because whether a particular
# comparable sale appears is a recall question and whether the facility limit
# appears is not.
#
# A fact that VANISHES is the failure a fact sheet cannot have: nothing points
# at it, so nobody can correct it. A fact two reads disagree about is a
# different thing entirely - `consensus.merge` marks it contested and it is
# FLAGGED for review, which is a question put to a person rather than an error.
# So divergence does not fail this bar; disappearance does.


class MaterialIntact(BaseModel):
    """Whether the facts a credit paper is built from survived every read."""

    runs: int
    paths: int
    missing: tuple[str, ...] = ()
    diverging: tuple[str, ...] = ()
    note: str = ""

    @property
    def passes(self) -> bool:
        if self.note:
            # Unproven is not passed, the same rule as `Stability`.
            return False
        return not self.missing

    @property
    def intact(self) -> int:
        return self.paths - len(self.missing)


def _is_material_scalar(path: str) -> bool:
    from creditpaper.model.concepts import concept_for

    if "[" in path:
        return False
    concept = concept_for(path)
    return bool(concept and concept.material)


def material_intact_from(report: DeterminismReport) -> MaterialIntact:
    """The material-scalar bar, read off the same measurement as `Stability`.

    Takes the SAME `DeterminismReport`, so the two bars can never disagree
    about what was observed — only about what it means.
    """
    flapping = sorted(p.path for p in report.flapping if _is_material_scalar(p.path))
    # Listed separately rather than unioned: the two carry different types, and
    # spreading them into one list widens the element type to `object`.
    diverging = sorted(
        [d.path for d in report.value_divergent if _is_material_scalar(d.path)]
        + [a.path for a in report.anchor_divergent if _is_material_scalar(a.path)]
    )
    counted = {
        p
        for p in [
            *report.stable,
            *(p.path for p in report.flapping),
            *(d.path for d in report.value_divergent),
            *(a.path for a in report.anchor_divergent),
        ]
        if _is_material_scalar(p)
    }
    return MaterialIntact(
        runs=report.n_runs,
        paths=len(counted),
        missing=tuple(flapping),
        diverging=tuple(diverging),
        note=report.note,
    )


def assert_material_intact(result: MaterialIntact) -> None:
    """Raise naming the facts that vanished, because that is what to go and look at."""
    if result.passes:
        return
    if result.note:
        raise AssertionError(f"determinism not measured: {result.note}")
    raise AssertionError(
        f"{len(result.missing)} of {result.paths} material scalar fact(s) went missing "
        f"between {result.runs} reads of the same documents: " + ", ".join(result.missing)
    )
