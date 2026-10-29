"""Create the minimal local fixtures/ scaffold for validation deals (Build Plan §14).

Run:  .venv\\Scripts\\python scripts\\scaffold_fixtures.py

Deliberately unstructured: sorting documents into slots is the PRODUCT's job
(D3 — the drop zone proposes the mapping, the user confirms), so deal files
are dumped flat, original filenames, SharePoint-style. A messy pile is exactly
the input the intake flow has to handle; pre-sorting fixtures would test less.

Only four boundaries matter, all enforced by folders:
  1. which deal a file belongs to        -> one folder per deal
  2. which document is the signed paper  -> paper_signed.docx next to the README
  3. prior papers separate from deals    -> prior_papers/ (D10)
  4. build deal separate from blind set  -> blind/ is never tuned against (D11)

Everything under fixtures/ except README.md is gitignored tenant data
(D7/D12) — the tree is local-only; this script is what's committed.
Idempotent: existing files and folders are never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

GROUND_TRUTH_TEMPLATE: dict = {
    "_instructions": (
        "Track 1 ground truth (harness/reconcile.py). Needed before the first scoring "
        "run, not before dropping files. Curate every figure the signed paper states, "
        "once, by hand; a second person checks. Keys are deal-model paths where one "
        "exists, otherwise a clear figure label. Values are strings at full precision "
        "(Decimal — never floats). Pre-agreed explainable differences go in "
        "explainable_differences with the reason."
    ),
    "deal_name": "",
    "deal_type": "",
    "curated_by": "",
    "checked_by": "",
    "curated_date": "",
    "figures": {},
    "explainable_differences": {},
}

DEAL_README = """# Validation deal — local only, never committed (D7/D12)

Drop here:
- `paper_signed.docx` — the signed credit paper, next to this README.
- everything else — into `deal_files/`, flat, original filenames, any mess.
  Don't sort: mapping files to slots is the product's job (D3).
- `ground_truth.json` — fill in later, before the first Track 1 run.

Two exclusions (refused at intake anyway): no raw identity documents — the
screening conclusions stand in for them (D7) — and no pricing xlsx (pricing
comes from the signed ITS, DECISIONS.md 2026-06-10).
"""

BLIND_README = """# Blind set — harness only, never used in tuning (D11)

One folder per blind deal, same contents as build_deal. Pick deals deliberately
DIFFERENT from the build deal (type, valuer, sponsor) — the blind set's job is
to catch overfitting. Folder names are placeholders; rename to the real deals.
"""

PRIOR_PAPERS_README = """# Prior papers — House Style Profile source (D10)

5–10 approved credit papers, ideally spanning deal types and authors. These
calibrate structure, voice, and boilerplate only; visuals come from the
website Brand Profile, and website copy never calibrates prose (D10).
"""


def ensure(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_if_absent(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def scaffold_deal(root: Path) -> None:
    ensure(root / "deal_files")
    write_if_absent(root / "README.md", DEAL_README)
    write_if_absent(
        root / "ground_truth.json",
        json.dumps(GROUND_TRUTH_TEMPLATE, indent=2, ensure_ascii=False) + "\n",
    )


def main() -> None:
    scaffold_deal(FIXTURES / "build_deal")
    ensure(FIXTURES / "blind")
    write_if_absent(FIXTURES / "blind" / "README.md", BLIND_README)
    for name in ("blind_01", "blind_02", "blind_03"):
        scaffold_deal(FIXTURES / "blind" / name)
    ensure(FIXTURES / "prior_papers")
    write_if_absent(FIXTURES / "prior_papers" / "README.md", PRIOR_PAPERS_README)
    print(f"fixtures scaffold ready at {FIXTURES}")


if __name__ == "__main__":
    main()
