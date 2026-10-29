"""Re-cut a saved determinism run. Costs nothing, and answers a better question.

`ingest/determinism_gate.py` scores ONE number over every path either read
emitted, against `STABILITY_BAR`. Measured on real corpora that number is
nearly unmoveable and nearly uninformative, and both facts have the same cause:
most paths are COLLECTION ROWS — individual comparable sales, macro-factor
bullets — so reading more finds more of them and the denominator grows with
recall. On blind_03, 372 of 404 paths were collection rows.

Underneath that, the fact sheet a credit paper is built from behaves quite
differently. This cuts a dumped run four ways:

    all         what STABILITY_BAR scores today
    plain       scalars only, no collection rows
    material    concepts the registry flags material
    both        material AND plain — the paper's own numbers

Measured 2026-09-24 on blind_03 (300 windows, 3 runs), the four cuts moved from
28.0 / 25.0 / 15.4 / 22.2 at one pass to 30.3 / 45.9 / 40.0 / 75.0 at three.
The all-paths figure barely moved; the one that decided `DEFAULT_PASSES = 3`
was that material scalar facts stopped flapping entirely.

USAGE
    python scripts/general_extraction_determinism.py ... --dump runs.json
    python scripts/stability_cut.py runs.json

WHAT THIS PRINTS: counts, rates and field-path PREFIXES with collection keys
redacted. No values, no document text, no filenames.

The DUMP ITSELF is tenant data — it holds values and clause text — so keep it
outside the repo, the same rule as `fixtures/`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from creditpaper.ingest.determinism_gate import STABILITY_BAR  # noqa: E402
from creditpaper.model.concepts import concept_for  # noqa: E402

_KEY = re.compile(r"\[[^\]]*\]")


def redact(path: str) -> str:
    return _KEY.sub("[...]", path)


def bucket(runs: list[dict], path: str) -> str:
    """stable / flapping / value divergent / anchor divergent.

    The same four the gate defines: present every run with the same value and
    the same anchor is stable; absent from any run is flapping; present
    everywhere with different values is the dangerous one, because it looks
    settled in any single run.
    """
    present = [r for r in runs if path in r]
    if len(present) != len(runs):
        return "flapping"
    if len({str(r[path]["value"]) for r in present}) > 1:
        return "value divergent"
    if len({(r[path]["page"], r[path]["location"]) for r in present}) > 1:
        return "anchor divergent"
    return "stable"


def is_material(path: str) -> bool:
    concept = concept_for(path)
    return bool(concept and concept.material)


def report(label: str, paths: list[str], buckets: dict[str, str]) -> None:
    if not paths:
        print(f"\n{label}: no paths")
        return
    counts = Counter(buckets[p] for p in paths)
    stable = counts.get("stable", 0)
    share = stable / len(paths)
    verdict = "PASS" if share >= STABILITY_BAR else "FAIL"
    print(f"\n{label}: {len(paths)} paths, {stable} stable = {share:.1%}  [{verdict}]")
    for name, n in counts.most_common():
        print(f"    {name:<18} {n:>5}  {n / len(paths):>6.1%}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", help="the JSON written by --dump (tenant data; keep it out of the repo)")
    parser.add_argument("--names", action="store_true", help="list the plain paths per bucket, redacted")
    args = parser.parse_args()

    path = Path(args.dump)
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 2
    runs: list[dict] = json.loads(path.read_text(encoding="utf-8"))
    if len(runs) < 2:
        print("a stability cut needs at least two runs", file=sys.stderr)
        return 2

    all_paths = sorted({p for run in runs for p in run})
    buckets = {p: bucket(runs, p) for p in all_paths}
    plain = [p for p in all_paths if "[" not in p]
    material = [p for p in all_paths if is_material(p)]

    print(f"{len(runs)} runs, {len(all_paths)} distinct paths, bar {STABILITY_BAR:.0%}")
    report("ALL PATHS (what STABILITY_BAR scores today)", all_paths, buckets)
    report("PLAIN (scalars; no collection rows)", plain, buckets)
    report("MATERIAL (concept registry flag)", material, buckets)
    report("MATERIAL AND PLAIN (the paper's own numbers)", [p for p in plain if is_material(p)], buckets)

    # Flapping is a fact GOING MISSING between reads, which is the failure a
    # fact sheet cannot have. Divergence is two renderings of one fact, which
    # consensus marks contested and sends to review. They are not the same
    # defect and the headline share hides which one you have.
    both = [p for p in plain if is_material(p)]
    if both:
        missing = sum(1 for p in both if buckets[p] == "flapping")
        print(f"\nmaterial scalar facts that GO MISSING between reads: {missing} of {len(both)}")

    if args.names:
        print("\nplain paths by bucket (keys redacted):")
        for name in ("stable", "flapping", "value divergent", "anchor divergent"):
            rows = sorted(redact(p) for p in plain if buckets[p] == name)
            if rows:
                print(f"  {name} ({len(rows)}):")
                for row in rows:
                    print(f"    {'M' if is_material(row.replace('[...]', '[x]')) else ' '} {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
