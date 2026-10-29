"""Run one deal folder under each candidate model and print the comparison.

Never defaults to a fixture path: the folder is an argument, because fixtures
are tenant data and a default is how they end up in a CI log.

Accuracy comes from Track 1's own reconciliation (`harness/reconcile.py`), so
this and `scripts/validate_track1.py` agree about what a match is by
construction rather than by coincidence.

Usage:
    python scripts/ingest_accuracy.py --folder "$CREDITPAPER_DEAL_DOCS" \
        --ground-truth path/to/ground_truth.json --deal-id deal-under-test
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from creditpaper.harness.reconcile import GroundTruth, reconcile  # noqa: E402
from creditpaper.ingest.scorecard import candidate_labels, render_table, score_run  # noqa: E402
from creditpaper.llm.registry import Task  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, help="deal documents folder")
    parser.add_argument("--ground-truth", required=True, help="curated expected values (JSON)")
    parser.add_argument("--deal-id", required=True, help="deal id to run under")
    parser.add_argument("--task", default="extract", choices=[t.value for t in Task])
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        raise SystemExit(f"not a folder: {args.folder}")
    truth = GroundTruth.load(args.ground_truth)

    task = Task(args.task)
    previous = os.environ.get(f"LLM_TASK_{task.name}")
    scores = []
    try:
        for label in candidate_labels(task):
            os.environ[f"LLM_TASK_{task.name}"] = label
            started = time.monotonic()

            from creditpaper.ingest.harness_run import run_deal_folder

            model, cost_usd = run_deal_folder(folder, deal_id=args.deal_id)
            report = reconcile(model, truth)
            scores.append(score_run(label, report, cost_usd=cost_usd, seconds=time.monotonic() - started))
    finally:
        # Leave the environment as it was found: this script is run repeatedly
        # in a shell, and a leaked override would silently change the next run.
        if previous is None:
            os.environ.pop(f"LLM_TASK_{task.name}", None)
        else:
            os.environ[f"LLM_TASK_{task.name}"] = previous

    print(render_table(scores))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
