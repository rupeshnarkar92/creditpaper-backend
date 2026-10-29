"""Build a redacted, evidence-only false-flag calibration report.

This tool never changes thresholds. It identifies review-reason/family buckets
that have enough analyst decisions to enter a shadow evaluation. Values, quotes,
document names, and source text are never emitted.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

NEVER_RELAX = frozenset(
    {
        "CONTRADICTION",
        "CURRENCY_MISMATCH",
        "KIND_UNPROVEN",
        "NEGATED",
        "FIGURES_DIFFER",
        "WRONG_COLUMN",
        "DIFFERS_FROM_CURRENT",
    }
)


def _family(path: str) -> str:
    return re.split(r"[.\[]", path, maxsplit=1)[0] or "other"


def calibration_report(
    labels: list[Any], *, min_bucket_labels: int = 73, min_reason_coverage: float = 0.95
) -> dict[str, Any]:
    """Return aggregate evidence and shadow candidates, never runtime changes."""
    from creditpaper.ledger.labels import values_agree

    latest: dict[tuple[str, str], Any] = {}
    for label in labels:
        if label.source == "analyst":
            latest[(label.deal_id, label.concept_path)] = label

    buckets: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"labels": 0, "correct": 0, "wrong": 0}
    )
    held = classified = 0
    for label in latest.values():
        if label.passed:
            continue
        held += 1
        reasons = tuple(label.verify_reasons or ())
        if reasons:
            classified += 1
        else:
            reasons = ("UNCLASSIFIED_LEGACY",)
        correct = label.final_value is not None and values_agree(label.extracted_value, label.final_value)
        for reason in reasons:
            bucket = buckets[(_family(label.concept_path), reason)]
            bucket["labels"] += 1
            bucket["correct" if correct else "wrong"] += 1

    reason_coverage = round(classified / held, 3) if held else 1.0
    rendered = []
    candidates = []
    for (family, reason), counts in sorted(buckets.items()):
        row = {"family": family, "reason": reason, **counts}
        row["false_flag_rate"] = round(counts["correct"] / counts["labels"], 3)
        eligible = (
            reason_coverage >= min_reason_coverage
            and reason not in NEVER_RELAX
            and reason != "UNCLASSIFIED_LEGACY"
            and counts["labels"] >= min_bucket_labels
            and counts["wrong"] == 0
        )
        row["shadow_candidate"] = eligible
        rendered.append(row)
        if eligible:
            candidates.append({"family": family, "reason": reason})

    return {
        "mode": "evidence_only",
        "labels": len(latest),
        "held_labels": held,
        "reason_coverage": reason_coverage,
        "minimum_reason_coverage": min_reason_coverage,
        "minimum_bucket_labels": min_bucket_labels,
        "buckets": rendered,
        "shadow_candidates": candidates,
        "runtime_changes": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--min-bucket-labels", type=int, default=73)
    args = parser.parse_args(argv)

    from creditpaper.db import tenant_uuid
    from creditpaper.ledger.labels import labels_for

    tenant_id = str(tenant_uuid(args.tenant))
    try:
        labels = labels_for(tenant_id, latest_only=True)
    except Exception as exc:  # CLI boundary: report infrastructure failure without a traceback.
        parser.exit(3, f"could not read tenant labels: {exc}\n")
    report = calibration_report(labels, min_bucket_labels=args.min_bucket_labels)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: report[key] for key in ("labels", "held_labels", "reason_coverage", "shadow_candidates")},
            indent=2,
        )
    )
    return 0 if report["shadow_candidates"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
