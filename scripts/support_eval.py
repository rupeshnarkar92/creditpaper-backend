"""Score the semantic support check (ingest/support.py) per candidate model.

    uv run python scripts/support_eval.py [--models provider:model,...] [--budget-usd 3]
                                          [--env-file PATH] [--out FILE.json]

Runs every case of scripts/support_eval_set.py (invented negation and role
sentences, labelled yes/no) through ``check_support`` for each model, and prints
per model: accuracy, false supports (labelled no, answered a confident yes: what
would pass a wrong fact), missed supports (labelled yes, not answered a confident
yes: review load), unclear and invalid replies, reason-code agreement on the
"no" cases, per-category accuracy, cost and calls. Without --models it scores the
model ``registry.resolve(Task.SUPPORT)`` gives (EXTRACT's by default).

A model is chosen through the registry (LLM_TASK_SUPPORT set for its run), so the
registry's rules apply: an unknown or providerless entry ("typesafe:jev") is
reported as skipped. Spend is read from the cost ledger after every call and the
run stops once --budget-usd is reached. Real model calls: not for CI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scripts.support_eval_set import AFTER, BEFORE, CASES, Case  # noqa: E402

from creditpaper.ingest.support import SupportOutcome, check_support  # noqa: E402
from creditpaper.ingest.verify import SUPPORT_CONFIDENCE_FLOOR  # noqa: E402
from creditpaper.llm.registry import ModelChoice, Task, resolve  # noqa: E402
from creditpaper.obs.ledger import cost_session  # noqa: E402

# (case, model choice) -> outcome. The real one calls check_support; tests pass fakes.
Ask = Callable[[Case, ModelChoice], SupportOutcome]


def real_ask(case: Case, choice: ModelChoice) -> SupportOutcome:
    return check_support(
        case.field,
        case.definition,
        case.value,
        case.sentence,
        BEFORE + case.sentence + AFTER,
        kind=case.kind,
        choice=choice,
    )


@dataclass
class ModelScore:
    model: str
    cases: int = 0
    correct: int = 0
    false_support: int = 0
    missed_support: int = 0
    unclear: int = 0
    invalid: int = 0
    errors: int = 0  # the call raised (transport, truncation): counted wrong
    reasked: int = 0
    reason_agree: int = 0
    reason_total: int = 0
    by_category: dict[str, list[int]] = field(default_factory=dict)  # category -> [correct, total]
    cost_usd: float = 0.0
    calls: int = 0
    wrong: list[str] = field(default_factory=list)  # case ids, never text
    skipped: str | None = None
    stopped_at_budget: bool = False

    @property
    def accuracy(self) -> float:
        return self.correct / self.cases if self.cases else 0.0


def _passes(outcome: SupportOutcome) -> bool:
    """What verify_fact would treat as support: a "yes" at or above the floor."""
    return outcome.supports == "yes" and outcome.confidence >= SUPPORT_CONFIDENCE_FLOOR


def score_case(score: ModelScore, case: Case, outcome: SupportOutcome) -> None:
    passed = _passes(outcome)
    ok = passed if case.expected == "yes" else outcome.supports == "no"
    score.cases += 1
    score.correct += ok
    cat = score.by_category.setdefault(case.category, [0, 0])
    cat[0] += ok
    cat[1] += 1
    if not ok:
        score.wrong.append(f"{case.id}:{outcome.supports}")
    if case.expected == "no" and passed:
        score.false_support += 1
    if case.expected == "yes" and not passed:
        score.missed_support += 1
    score.unclear += outcome.supports == "unclear"
    score.invalid += not outcome.valid
    score.reasked += outcome.reasked
    if case.expected == "no" and outcome.supports == "no" and case.expected_reason:
        score.reason_total += 1
        score.reason_agree += getattr(outcome.reason_code, "value", None) == case.expected_reason


def score_model(
    spec: str | None,
    cases: Sequence[Case],
    ask: Ask,
    *,
    budget_usd: float,
    spent_usd: float = 0.0,
) -> ModelScore:
    """Score one model (``provider:model``, or None for the registry default)."""
    previous = os.environ.get("LLM_TASK_SUPPORT")
    try:
        if spec is not None:
            os.environ["LLM_TASK_SUPPORT"] = spec
        try:
            choice = resolve(Task.SUPPORT)
        except ValueError as exc:
            return ModelScore(model=spec or "default", skipped=str(exc).splitlines()[0])
    finally:
        if previous is None:
            os.environ.pop("LLM_TASK_SUPPORT", None)
        else:
            os.environ["LLM_TASK_SUPPORT"] = previous
    score = ModelScore(model=f"{choice.provider}:{choice.model}")
    with cost_session(deal_id="support-eval") as ledger:
        for case in cases:
            if spent_usd + ledger.total_usd >= budget_usd:
                score.stopped_at_budget = True
                break
            try:
                outcome = ask(case, choice)
            except Exception as exc:  # noqa: BLE001 - one failed call must not lose the run
                score.cases += 1
                score.errors += 1
                score.wrong.append(f"{case.id}:error({type(exc).__name__})")
                cat = score.by_category.setdefault(case.category, [0, 0])
                cat[1] += 1
                if case.expected == "yes":
                    score.missed_support += 1
                continue
            score_case(score, case, outcome)
        score.cost_usd = ledger.total_usd
        score.calls = ledger.calls
    return score


def render(scores: Sequence[ModelScore]) -> str:
    lines = [
        f"{'model':45} {'acc':>6} {'n':>4} {'false+':>6} {'missed':>6} {'unclr':>5} {'inval':>5} "
        f"{'err':>4} {'reason':>7} {'cost$':>7} {'calls':>5}"
    ]
    for s in scores:
        if s.skipped:
            lines.append(f"{s.model:45} skipped: {s.skipped}")
            continue
        reason = f"{s.reason_agree}/{s.reason_total}"
        lines.append(
            f"{s.model:45} {s.accuracy:6.1%} {s.cases:4d} {s.false_support:6d} {s.missed_support:6d} "
            f"{s.unclear:5d} {s.invalid:5d} {s.errors:4d} {reason:>7} {s.cost_usd:7.4f} {s.calls:5d}"
            + ("  (stopped at budget)" if s.stopped_at_budget else "")
        )
        cats = ", ".join(f"{c} {ok}/{n}" for c, (ok, n) in sorted(s.by_category.items()))
        lines.append(f"{'':45} by category: {cats}")
        if s.wrong:
            lines.append(f"{'':45} wrong: {' '.join(s.wrong)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, ask: Ask = real_ask) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--models", default="", help="comma-separated provider:model; default: the registry's")
    ap.add_argument("--budget-usd", type=float, default=3.0)
    ap.add_argument("--env-file", type=Path, default=None, help="a .env to load (API keys)")
    ap.add_argument("--out", type=Path, default=None, help="write the scores as JSON here")
    ap.add_argument("--limit", type=int, default=0, help="score only the first N cases")
    args = ap.parse_args(argv)
    if args.env_file is not None:
        from dotenv import load_dotenv

        load_dotenv(args.env_file)
    specs: list[str | None] = [m.strip() for m in args.models.split(",") if m.strip()] or [None]
    cases = CASES[: args.limit] if args.limit else CASES
    counts = Counter(c.expected for c in cases)
    print(f"{len(cases)} cases ({counts['yes']} yes, {counts['no']} no); budget ${args.budget_usd:.2f}")
    scores: list[ModelScore] = []
    spent = 0.0
    for spec in specs:
        s = score_model(spec, cases, ask, budget_usd=args.budget_usd, spent_usd=spent)
        spent += s.cost_usd
        scores.append(s)
    print(render(scores))
    print(f"total cost ${spent:.4f}")
    if args.out is not None:
        payload: list[dict[str, Any]] = [asdict(s) | {"accuracy": s.accuracy} for s in scores]
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
