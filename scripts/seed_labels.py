"""Seed a tenant's label store from answers an analyst has already confirmed (spec 3c).

    uv run python scripts/seed_labels.py --tenant <uuid> --ground-truth <gt.json> [--deal-id ID]
    uv run python scripts/seed_labels.py --tenant <uuid> --signed-paper <paper>   (not built yet)

Until the feedback loop has data, the answer key is bootstrapped from the fixture
ground-truth files and from signed credit papers. Every seed label is marked
source=seed, action=accepted, with no extracted value and no deciding user: it says
what the answer is, not what any extractor produced. A seed that disagrees with the
documents is resolved once by an analyst in the Fact Sheet, and that decision is
appended as an analyst label, which then supersedes the seed as the answer key.

Re-running is safe: a concept already seeded with the same value for the deal is
skipped (labels are append-only, so a changed value appends a new seed label).
Output is counts only, never values: fixtures are tenant data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from creditpaper.harness.reconcile import _RATIO_TO_PATH  # noqa: E402

SECTIONS = ("figures_numeric", "derived_expected", "reference_values")
EXPLAINABLE = "explainable_difference"
SIGNED_PAPER_NEEDS = (
    "Seeding from a signed credit paper is not built yet. It needs: (1) a parser that reads "
    "the signed paper's figures and maps each to a registry concept path (Plan C Task 9 "
    "concept registry); (2) the paper's approval reference, so each seed label can cite the "
    "signed version it came from; (3) an analyst pass on concepts the paper states "
    "differently from the documents, resolved once in the Fact Sheet."
)


def seeds_from_ground_truth(data: dict[str, Any]) -> list[dict[str, Any]]:
    """(concept_path, value, reason_code) for every checked ground-truth value."""
    explainable = {k for k in (data.get("explainable_differences") or {}) if not str(k).startswith("_")}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(path: str, value: Any) -> None:
        if path.startswith("_") or path in seen or value is None:
            return
        seen.add(path)
        out.append(
            {
                "concept_path": path,
                "final_value": value,
                "reason_code": EXPLAINABLE if path in explainable else None,
            }
        )

    for section in SECTIONS:
        for path, value in (data.get(section) or {}).items():
            add(str(path), value)
    for key, value in (data.get("computed_ratios") or {}).items():
        path = _RATIO_TO_PATH.get(str(key))
        if path is not None:
            add(path, value)
    return out


def seed(tenant_id: str, deal_id: str, seeds: list[dict[str, Any]]) -> dict[str, int]:
    from creditpaper.db import tenant_cursor
    from creditpaper.ledger.labels import labels_for, record_label, values_agree

    existing = {
        lb.concept_path: lb.final_value
        for lb in labels_for(tenant_id, deal_id=deal_id, source="seed", latest_only=True)
    }
    written = skipped = 0
    with tenant_cursor(tenant_id) as cur:  # one transaction: all seeds or none
        for s in seeds:
            path = s["concept_path"]
            if path in existing and values_agree(existing[path], s["final_value"]):
                skipped += 1
                continue
            record_label(
                cur,
                tenant_id=tenant_id,
                deal_id=deal_id,
                concept_path=path,
                action="accepted",
                source="seed",
                final_value=s["final_value"],
                reason_code=s["reason_code"],
            )
            written += 1
    return {"written": written, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--tenant", required=True, help="tenant uuid the labels belong to")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--ground-truth", type=Path, help="a fixture deal's ground_truth.json")
    src.add_argument("--signed-paper", type=Path, help="a signed credit paper (not built yet)")
    ap.add_argument(
        "--deal-id", default=None, help="deal id to seed under (default: the fixture folder name)"
    )
    args = ap.parse_args(argv)

    from creditpaper.db import tenant_uuid

    try:
        tenant = str(tenant_uuid(args.tenant))
    except ValueError:
        ap.error("--tenant must be a uuid")
    if args.signed_paper is not None:
        print(SIGNED_PAPER_NEEDS)
        return 2
    path: Path = args.ground_truth
    if not path.is_file():
        ap.error("ground truth file not found")
    data = json.loads(path.read_text(encoding="utf-8"))
    deal_id = args.deal_id or path.resolve().parent.name
    result = seed(tenant, deal_id, seeds_from_ground_truth(data))
    print(f"seed labels: {result['written']} written, {result['skipped']} already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
