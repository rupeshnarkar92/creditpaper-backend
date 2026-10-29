"""Compare model choices on the reconciliation this product already runs.

Almost none of this is new. Accuracy against curated ground truth is Track 1's
job (D11) and lives in `harness/reconcile.py`: `GroundTruth`, `reconcile`, a
per-field `Line`, tolerance-aware `numbers_match`, and `reconciled_or_explained`
as the bar. Cost and latency are already captured on `IngestReport`, in a
`cost_session` around the run, where wall-clock is deliberately what the user
waits rather than the sum of per-document latencies.

What was missing is only the axis the product decision needs: the same deal, run
under several model choices, in one table. So this module scores a
`ReconcileReport` rather than re-deriving what a match is - one definition of
"same figure", or the harness and the verifier will eventually disagree about
one.

Two rules in the output. Missing and wrong are counted separately, because a
model that declines is not a model that invents and averaging them hides the
difference that matters. And the table prints field paths and counts only:
never document text, file names or party names, because the people reading a
model comparison should not be shown tenant data to do it.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from creditpaper.harness.reconcile import ReconcileReport, Verdict, numbers_match
from creditpaper.ingest.determinism_gate import Stability
from creditpaper.llm.registry import CANDIDATES, Task

__all__ = [
    "SAFE_OUTPUT_NOTE",
    "Stability",
    "RunScore",
    "candidate_labels",
    "numbers_match",
    "render_table",
    "score_run",
]

SAFE_OUTPUT_NOTE = "Field paths and counts only. No document text, file names or party names."


class RunScore(BaseModel):
    label: str
    checked: int
    matched: int
    wrong: int
    missing: int
    explained: int
    pass_rate: float
    wrong_paths: tuple[str, ...]
    cost_usd: float
    seconds: float
    # A model that scores well by flapping into the right answer should be
    # visible as such, so stability sits beside accuracy rather than in a
    # separate report nobody reads next to this one.
    stability: Stability | None = None


def score_run(
    label: str,
    report: ReconcileReport,
    *,
    cost_usd: float,
    seconds: float,
    stability: Stability | None = None,
) -> RunScore:
    """One model choice's result, from the Track 1 report it produced."""
    wrong = [ln for ln in report.lines if ln.verdict is Verdict.MISMATCH]
    # An empty report is not a perfect score: a run that checked nothing proves
    # nothing. ReconcileReport.pass_rate returns 1.0 for an empty run, which is
    # the right default for "nothing to reconcile" and the wrong one here.
    pass_rate = report.pass_rate if report.checked else 0.0
    return RunScore(
        label=label,
        checked=report.checked,
        matched=report.matched,
        wrong=len(wrong),
        missing=len(report.missing),
        explained=len(report.explained),
        pass_rate=pass_rate,
        wrong_paths=tuple(ln.path for ln in wrong),
        cost_usd=cost_usd,
        seconds=seconds,
        stability=stability,
    )


def render_table(scores: Sequence[RunScore]) -> str:
    header = (
        f"{'model choice':<28} {'ok':>7} {'stable':>7} {'checked':>8} {'wrong':>6} "
        f"{'missing':>8} {'cost $':>8} {'secs':>7}"
    )
    rows = [header, "-" * len(header)]
    for score in scores:
        stable = f"{score.stability.share:.0%}" if score.stability else "-"
        rows.append(
            f"{score.label:<28} {score.pass_rate:>6.1%} {stable:>7} {score.checked:>8} "
            f"{score.wrong:>6} {score.missing:>8} {score.cost_usd:>8.2f} {score.seconds:>7.0f}"
        )
    worst = [s for s in scores if s.wrong_paths]
    if worst:
        rows.append("")
        rows.append("wrong fields:")
        for score in worst:
            rows.append(f"  {score.label}: {', '.join(score.wrong_paths)}")
    rows.append("")
    rows.append(SAFE_OUTPUT_NOTE)
    return "\n".join(rows)


def candidate_labels(task: Task) -> list[str]:
    """The model choices worth scoring for a task, from the registry's own list."""
    return [label for label in CANDIDATES.get(task, ()) if label]
