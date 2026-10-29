"""Track-1 numeric validation runner (D11): reconcile a deal model against its curated
ground truth and print the "reconciled-or-explained" scorecard.

Two modes:
  --mode populate  (default, FREE): build the model from ground_truth.json via the
      populate path, derive, then reconcile. Tests the DERIVE logic and proves the
      harness runs across every deal/deal-type. (figures_numeric match trivially since
      they're populated from GT; derived_expected / computed_ratios are the real test.)
  --mode ingest (LIVE, costs API $): route + ingest the deal's documents, then
      reconcile the EXTRACTED model against GT — the real extraction-accuracy test.
      Differences are extraction errors OR explainable (e.g. ITS versioning).

  --deal {riverton,wattlefield,residual,all}

Run:  .venv/Scripts/python scripts/validate_track1.py --deal all
      .venv/Scripts/python scripts/validate_track1.py --deal riverton --mode ingest
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from creditpaper.harness.reconcile import GroundTruth, reconcile  # noqa: E402
from creditpaper.model import DealModel, derive_economics, derive_metrics  # noqa: E402

_DEALS = {
    "riverton": ("fixtures/build_deal/ground_truth.json", "fixtures/build_deal/deal_files", "riverton"),
    "wattlefield": (
        "fixtures/blind/blind_01/ground_truth.json",
        "fixtures/blind/blind_01/deal_files",
        "wattlefield",
    ),
    "residual": (
        "fixtures/blind/blind_03/ground_truth.json",
        "fixtures/blind/blind_03/deal_files",
        "residual",
    ),
}


def _model_populate(gt_path: Path) -> DealModel:
    from creditpaper.harness.populate import populate_from_ground_truth

    model, _ = populate_from_ground_truth(gt_path)
    derive_economics(model)
    derive_metrics(model)
    return model


def _model_ingest(deal_dir: Path, deal_id: str) -> DealModel:
    from creditpaper.harness.ingest import ingest

    from creditpaper.harness.route import route_deal

    files = [str(p) for p in deal_dir.rglob("*.pdf")] + [str(p) for p in deal_dir.rglob("*.PDF")]
    mapping = route_deal(sorted(set(files)))
    model = DealModel(tenant_id="hyperion", deal_id=deal_id)
    rep = ingest(model, mapping)
    print(
        f"  ingested {len(rep.applied)} facts from {sorted(mapping)}; "
        f"${rep.cost_usd:.4f}, {rep.wall_clock_s:.0f}s"
    )
    return model


def _run_one(deal: str, mode: str) -> bool:
    gt_rel, dir_rel, deal_id = _DEALS[deal]
    gt = GroundTruth.load(ROOT / gt_rel)
    print(f"\n{'=' * 78}\nDEAL {deal} ({mode}) — {gt.deal_name}")
    if not gt.figures_numeric and not gt.reference_values:
        print("  ground truth not curated yet — skipping")
        return True
    model = _model_ingest(ROOT / dir_rel, deal_id) if mode == "ingest" else _model_populate(ROOT / gt_rel)
    rep = reconcile(model, gt)
    print(rep.summary())
    return rep.reconciled_or_explained


def main() -> int:
    load_dotenv(ROOT / ".env")
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "populate"
    deal = sys.argv[sys.argv.index("--deal") + 1] if "--deal" in sys.argv else "all"
    deals = list(_DEALS) if deal == "all" else [deal]

    results = {d: _run_one(d, mode) for d in deals}
    print(f"\n{'=' * 78}\nTRACK-1 SUMMARY ({mode}):")
    for d, ok in results.items():
        print(f"  {d:10} {'PASS (reconciled-or-explained)' if ok else 'DISCREPANCIES — see above'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
