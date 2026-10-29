"""Full ingestion run on a deal (`--deal`) — the end-to-end
plumbing: route the whole file pile -> canonical doc per slot -> parallel extract
-> apply -> derive -> reconcile, with the cost ledger open.

Prints the proposed routing, the IngestReport (facts/flags/reconciliation), the
derived crux (LVR/LCR), and the per-deal cost + wall-clock. Fixtures are tenant
data (gitignored) — nothing here is committed. Run:
  .venv/Scripts/python scripts/ingest_deal.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from creditpaper.harness.ingest import ingest  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from creditpaper.harness.route import classify, route_deal  # noqa: E402
from creditpaper.model import DealModel  # noqa: E402

_DEALS = {
    "riverton": ("fixtures/build_deal/deal_files", "riverton-kbl01"),
    "wattlefield": ("fixtures/blind/blind_01/deal_files", "wattlefield-wbr01"),
}


def _deal() -> tuple[Path, str]:
    key = sys.argv[sys.argv.index("--deal") + 1] if "--deal" in sys.argv else "riverton"
    d, did = _DEALS.get(key, (key, "deal"))
    return ROOT / d, did


def _fnum(fact) -> str:
    v = getattr(fact, "value", None)
    return f"{v}" if v is not None else "—"


def main() -> int:
    load_dotenv(ROOT / ".env")
    DEAL_DIR, deal_id = _deal()
    if not DEAL_DIR.exists():
        print(f"missing deal dir: {DEAL_DIR}")
        return 1

    files = [str(p) for p in DEAL_DIR.rglob("*.pdf")] + [str(p) for p in DEAL_DIR.rglob("*.PDF")]
    files = sorted(set(files))
    classified = sum(1 for f in files if classify(f) is not None)
    mapping = route_deal(files)

    print(
        f"Deal pile: {len(files)} PDFs — {classified} classified to a known slot, "
        f"{len(mapping)} routed to an extractor we have.\n"
    )
    print("Routed canonical document per slot:")
    for slot, docs in sorted(mapping.items()):
        print(f"  [{slot}] {', '.join(Path(p).name for p in docs)}")

    model = DealModel(tenant_id="hyperion", deal_id=deal_id)
    print("\nExtracting (parallel across docs; valuation runs 3 passes over its cached PDF)…")
    rep = ingest(model, mapping)

    print(f"\n=== INGEST REPORT — reconciled: {rep.reconciled} ===")
    print(f"facts by slot: {rep.facts_by_slot}")
    print(f"applied: {len(rep.applied)} facts to the model")
    if rep.extractor_flags:
        print(f"\nextractor flags ({len(rep.extractor_flags)}):")
        for fl in rep.extractor_flags[:20]:
            print(f"  - {fl}")
    if rep.reconciliation:
        print(f"\nreconciliation — held for human ({len(rep.reconciliation)}):")
        for r in rep.reconciliation:
            print(f"  ! {r}")
    else:
        print("\nreconciliation: clean — nothing held")

    e, d = model.economics, model.metrics
    print("\n=== DERIVED CRUX ===")
    print(f"  facility:        {_fnum(e.facility_amount)}")
    print(f"  as-is value:     {_fnum(model.valuation.values_by_basis.get('as_is'))}")
    print(f"  as-if-complete:  {_fnum(model.valuation.values_by_basis.get('as_if_complete'))}")
    print(f"  day-1 LVR:       {_fnum(getattr(d, 'asis_lvr_pct', None))}%")
    print(f"  completed LVR:   {_fnum(getattr(d, 'lvr_pct', None))}%")
    print(f"  LCR:             {_fnum(getattr(d, 'lcr_pct', None))}%")
    print(f"  comparables:     {len(model.valuation.comp_set)}")
    print(f"  covenants:       {list(model.covenants)}")

    print(f"\n=== COST OF SALE (ingestion) — ${rep.cost_usd:.4f}, {rep.wall_clock_s:.0f}s ===")
    print(rep.cost_summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
