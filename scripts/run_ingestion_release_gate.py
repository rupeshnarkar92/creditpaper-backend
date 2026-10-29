"""Produce one redacted ingestion release-evidence bundle for a tenant."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from accuracy_label_plan import build_plan  # noqa: E402
from false_flag_calibration import calibration_report  # noqa: E402
from ingest_scorecard import main as scorecard_main  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--performance-summary", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("var/scorecard/release"))
    args = parser.parse_args(argv)

    from creditpaper.db import tenant_uuid
    from creditpaper.ledger.labels import labels_for

    tenant_id = str(tenant_uuid(args.tenant))
    try:
        labels = labels_for(tenant_id, latest_only=True)
    except Exception as exc:  # CLI boundary: report infrastructure failure without a traceback.
        parser.exit(3, f"could not read tenant labels: {exc}\n")
    args.out.mkdir(parents=True, exist_ok=True)
    plan = build_plan(labels)
    calibration = calibration_report(labels)
    (args.out / "label-coverage.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    (args.out / "false-flag-calibration.json").write_text(json.dumps(calibration, indent=2), encoding="utf-8")
    score_result = scorecard_main(
        [
            "--labels",
            f"tenant={tenant_id}",
            "--performance-summary",
            str(args.performance_summary),
            "--out",
            str(args.out),
            "--production-gate",
        ]
    )
    bundle = {
        "passed": score_result == 0 and plan["ready_for_scoring"],
        "coverage_ready": plan["ready_for_scoring"],
        "scorecard_exit_code": score_result,
        "shadow_candidates": calibration["shadow_candidates"],
        "artifacts": [
            "label-coverage.json",
            "false-flag-calibration.json",
            "aggregate.json",
            "release-verdict.json",
        ],
    }
    (args.out / "evidence-bundle.json").write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    print(json.dumps(bundle, indent=2))
    return 0 if bundle["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
