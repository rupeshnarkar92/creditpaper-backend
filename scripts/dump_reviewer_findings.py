"""Diagnostic (read-only): materialise the claims-fidelity reviewer's findings on
the current cached prose so calibration can be judged from DATA, not assumption.

Tests the foundational question before any tuning: are the ~6–11 findings/section
false positives (reviewer too strict), real issues (drafts over-claim), or just a
presentation problem? Dumps every finding (section, category, quote, explanation)
to output/reviewer_findings.json for labelling. Changes nothing in the reviewer.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402

from creditpaper.agents.review import ClaimsFidelityReviewer  # noqa: E402
from creditpaper.harness.populate import populate_from_ground_truth  # noqa: E402
from creditpaper.model.core import ProseBlock  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GT = ROOT / "fixtures" / "build_deal" / "ground_truth.json"
CACHE = ROOT / "output" / "riverton_prose.json"
OUT = ROOT / "output" / "reviewer_findings.json"


def main() -> None:
    load_dotenv(ROOT / ".env")
    model, _ = populate_from_ground_truth(GT)
    prose = json.loads(CACHE.read_text(encoding="utf-8"))
    reviewer = ClaimsFidelityReviewer()
    rows: list[dict] = []
    by_cat: dict[str, int] = {}
    for s, blk in sorted(prose.items(), key=lambda kv: int(kv[0])):
        block = ProseBlock(**blk)
        report = reviewer.review(model, block)
        print(f"section {s}: {len(report.findings)} findings", flush=True)
        for f in report.findings:
            by_cat[f.category] = by_cat.get(f.category, 0) + 1
            rows.append(
                {
                    "section": int(s),
                    "category": f.category,
                    "quote": f.quote,
                    "explanation": f.explanation,
                    "label": "",
                }
            )  # label: TRUE / FALSE / blank, for calibration
    OUT.write_text(json.dumps({"by_category": by_cat, "findings": rows}, indent=2), encoding="utf-8")
    print(f"\ntotal {len(rows)} findings; by category: {by_cat}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
