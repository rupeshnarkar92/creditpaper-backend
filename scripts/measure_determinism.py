"""Run-to-run determinism measurement (see harness/determinism.py for the analysis
core and the rationale: two live ingestion runs on the same deal produced 384 facts
then 370, and the qs_vs_funding reconciliation reported four divergences on one run
and one on the next — a "catch" that appears then vanishes destroys trust in all of
them). Runs ONE extractor over ONE document N times and reports which citation paths
are stable, which flap (present in some runs, absent in others), which agree on
presence but disagree on VALUE, and which agree on value but disagree on ANCHOR
(page/clause) — the four buckets `measure_determinism` produces.

This is the run-to-run CONSISTENCY instrument, a different axis from the accuracy
one (scripts/validate_track1.py, which diffs against curated ground truth). It needs
no ground truth, only repetition — and there is no sampling knob to reach for:
Opus 5 / Sonnet 5 accept no temperature/top_p/top_k, so the only lever is measuring
the variance precisely enough to localise it to specific field paths.

COSTS REAL API MONEY — every run is a live extraction call against the same
document. Opens its own cost_session and prints the ledger, so the cost of running
this instrument is as visible as the cost of the product itself.

Run:  .venv/Scripts/python scripts/measure_determinism.py --slot its --doc path/to/its.pdf
      .venv/Scripts/python scripts/measure_determinism.py --slot qs --doc path/to/qs.pdf --runs 5
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from creditpaper.harness.determinism import measure_determinism  # noqa: E402
from creditpaper.harness.route import EXTRACTOR_REGISTRY  # noqa: E402
from creditpaper.obs.ledger import cost_session  # noqa: E402


def _arg(name: str, default: str | None = None) -> str | None:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main() -> int:
    load_dotenv(ROOT / ".env")
    slot = _arg("--slot")
    doc = _arg("--doc")
    runs_n = int(_arg("--runs", "3"))

    if slot is None or doc is None:
        print("usage: measure_determinism.py --slot <extractor slot> --doc <path to pdf> [--runs N]")
        print(f"  known slots: {sorted(EXTRACTOR_REGISTRY)}")
        return 2
    if slot not in EXTRACTOR_REGISTRY:
        print(f"unknown slot {slot!r} — known slots: {sorted(EXTRACTOR_REGISTRY)}")
        return 2
    doc_path = Path(doc)
    if not doc_path.exists():
        print(f"document not found: {doc_path}")
        return 2

    extractor_cls = EXTRACTOR_REGISTRY[slot]
    # Same document_id on every run — it IS the same document each time; only the
    # model's response varies, which is the entire thing being measured.
    document_id = f"{slot}:{doc_path.name}"
    print(f"MEASURE DETERMINISM — slot={slot} doc={doc_path.name} runs={runs_n}")

    outputs = []
    with cost_session(deal_id=f"determinism-{slot}") as ledger:
        for i in range(runs_n):
            print(f"  run {i + 1}/{runs_n} …", flush=True)
            out = extractor_cls().extract(document_id, doc_path)
            outputs.append(out)
            print(f"    {len(out.fields)} facts, {len(out.flags)} flag(s)")

    print("\n" + ledger.summary())

    report = measure_determinism([out.fields for out in outputs])
    print(f"\n{report.summary_line()}")

    if report.note:
        # n_runs < 2 — nothing below is meaningful; the note already says why.
        return 0

    # Flapping and value-divergent paths are the actionable output (completeness and
    # accuracy failures respectively) — printed in full, sorted by path so two runs
    # of this script over the same document are diffable.
    if report.flapping:
        print(f"\nFLAPPING ({len(report.flapping)}) — present in some runs, absent in others:")
        for fp in sorted(report.flapping, key=lambda f: f.path):
            print(f"  {fp.path}: {fp.describe()}")
    else:
        print("\nFLAPPING: none")

    if report.value_divergent:
        print(f"\nVALUE-DIVERGENT ({len(report.value_divergent)}) — present every run, differing value:")
        for vd in sorted(report.value_divergent, key=lambda v: v.path):
            values_str = ", ".join(f"{v!r} x{c}" for v, c in sorted(vd.values.items()))
            print(f"  {vd.path}: {values_str}")
    else:
        print("\nVALUE-DIVERGENT: none")

    # Lower severity (the fact is right, only its citation moved) — still printed,
    # since under D4 a wandering anchor is a reproducibility failure in its own right.
    if report.anchor_divergent:
        print(f"\nANCHOR-DIVERGENT ({len(report.anchor_divergent)}) — same value, differing citation:")
        for ad in sorted(report.anchor_divergent, key=lambda a: a.path):
            anchors_str = ", ".join(f"{a!r} x{c}" for a, c in sorted(ad.anchors.items()))
            print(f"  {ad.path} = {ad.value!r}: {anchors_str}")

    print(f"\nSTABLE: {len(report.stable)}/{report.total_paths} paths ({report.stability_rate * 100:.1f}%)")

    # _doc.add_fact now flags every silent drop (D4 provenance failures AND bare
    # model omissions) instead of dropping quietly; a path that flaps above and
    # carries a "no value returned by the model" flag on the runs where it's absent
    # is the mechanism caught in the act, not a coincidence.
    all_flags = sorted({fl for out in outputs for fl in out.flags})
    if all_flags:
        print(f"\nEXTRACTOR FLAGS across all runs ({len(all_flags)} distinct):")
        for fl in all_flags:
            print(f"  {fl}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
