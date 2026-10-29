"""Release gate: a prompt, model or extractor change may not regress any concept (spec 3c).

    uv run python scripts/extraction_gate.py BASELINE CANDIDATE

BASELINE and CANDIDATE are scorecard results from scripts/ingest_scorecard.py: a
scorecard.json, or a directory holding one or more (one per deal; cache/ is ignored).
Score both against the same answer key (normally the label store: --labels).

Per concept, keyed by (deal, concept path), the gate fails on:
- a regression: correct in the baseline, and wrong or missing (or absent) in the
  candidate;
- a new false pass: FALSE_PASS in the candidate where the baseline was not.

Exit 0 when neither occurs, 1 when either does, 2 on unreadable input. Output is
concept paths and categories only, never values.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

UNSCORED = {"not_in_ground_truth"}


@dataclass(frozen=True)
class Outcome:
    category: str
    correct: bool


@dataclass(frozen=True)
class Finding:
    deal: str
    path: str
    kind: str  # regression | new_false_pass
    baseline: str
    candidate: str


def _files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise FileNotFoundError(str(root))
    return sorted(p for p in root.rglob("scorecard.json") if "cache" not in p.relative_to(root).parts)


def load(root: Path) -> dict[tuple[str, str], Outcome]:
    """(deal, concept) -> outcome, from every scorecard under `root`."""
    out: dict[tuple[str, str], Outcome] = {}
    files = _files(root)
    if not files:
        raise FileNotFoundError(f"no scorecard.json under {root}")
    for f in files:
        payload: dict[str, Any] = json.loads(f.read_text(encoding="utf-8"))
        deal = str(payload.get("deal") or f.parent.name)
        for row in payload.get("facts") or []:
            if row.get("category") in UNSCORED:
                continue
            out[(deal, str(row["path"]))] = Outcome(
                category=str(row["category"]), correct=row.get("correct") is True
            )
    return out


def compare(
    baseline: dict[tuple[str, str], Outcome], candidate: dict[tuple[str, str], Outcome]
) -> list[Finding]:
    findings: list[Finding] = []
    for key in sorted(set(baseline) | set(candidate)):
        before, after = baseline.get(key), candidate.get(key)
        b_cat = before.category if before else "absent"
        a_cat = after.category if after else "absent"
        if before is not None and before.correct and (after is None or not after.correct):
            findings.append(Finding(key[0], key[1], "regression", b_cat, a_cat))
        elif after is not None and after.category == "FALSE_PASS" and b_cat != "FALSE_PASS":
            findings.append(Finding(key[0], key[1], "new_false_pass", b_cat, a_cat))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("baseline", type=Path)
    ap.add_argument("candidate", type=Path)
    args = ap.parse_args(argv)
    try:
        baseline, candidate = load(args.baseline), load(args.candidate)
    except (OSError, ValueError, KeyError) as exc:
        print(f"gate: unreadable scorecard input ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2
    findings = compare(baseline, candidate)
    fixed = sum(
        1 for k, o in candidate.items() if o.correct and (k not in baseline or not baseline[k].correct)
    )
    print(
        f"gate: {len(baseline)} baseline concepts, {len(candidate)} candidate concepts, {fixed} newly correct"
    )
    for f in findings:
        print(f"  {f.kind:<15} {f.deal} {f.path}: {f.baseline} -> {f.candidate}")
    print("gate: FAIL" if findings else "gate: PASS")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
