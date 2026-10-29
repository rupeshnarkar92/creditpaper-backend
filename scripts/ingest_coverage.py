"""How much of a deal folder does ingestion actually read? No model calls, no cost.

The confidence probe measures whether the facts we GOT are trustworthy. This
measures how many we never had the chance to get — a different and, on the
evidence, larger problem. A 144-file folder produced 11 extractor runs.

Three places documents and pages are dropped, each silent:

  1. `classify()` returns None for a filename it cannot name, and `route_deal`
     skips it. The file is never read by anything.
  2. `route_deal` keeps ONE document per slot — `ranked[0]` — unless the slot is
     in MULTI_DOC_SLOTS (only insurance, kyc, sponsor_financials). Every other
     candidate is discarded, however relevant.
  3. `select_pages` caps at MAX_PAGES_PER_PASS (10). A document with 62 matching
     pages is read from 10 of them. This one at least logs.

Routing and page selection are pure functions of filenames and page text, so
this runs locally and spends nothing.

WHAT THIS PRINTS: counts and slot keys. Filenames are reduced to a per-slot
count — a deal filename names the borrower as readily as the document does.

Usage:
    python scripts/ingest_coverage.py --folder "$CREDITPAPER_DEAL_DOCS"
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SUPPORTED_SUFFIXES = {".pdf"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, help="deal documents folder")
    parser.add_argument("--pages", action="store_true", help="also parse, to measure page capping")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"not a folder: {folder}", file=sys.stderr)
        return 2

    from creditpaper.harness.route import (
        EXTRACTOR_REGISTRY,
        MULTI_DOC_SLOTS,
        classify,
        route_deal,
    )

    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
    if not files:
        print("no PDFs in that folder")
        return 1

    # --- stage 1: what does the filename classifier recognise? -----------------
    by_slot: Counter[str] = Counter()
    unclassified = 0
    no_extractor: Counter[str] = Counter()
    for f in files:
        slot = classify(f)
        if slot is None:
            unclassified += 1
        elif slot not in EXTRACTOR_REGISTRY:
            no_extractor[slot] += 1
        else:
            by_slot[slot] += 1

    # --- stage 2: what survives routing? ---------------------------------------
    routed = route_deal([str(f) for f in files])
    read = sum(len(v) for v in routed.values())

    print(f"\n{len(files)} documents in the folder\n")
    print("stage 1 — filename classification")
    print(f"  recognised, extractor exists   {sum(by_slot.values()):>5}")
    print(f"  recognised, no extractor       {sum(no_extractor.values()):>5}  {sorted(no_extractor) or ''}")
    print(f"  NOT recognised, dropped        {unclassified:>5}  ({unclassified / len(files):.0%})")

    print("\nstage 2 — routing (one document per slot unless multi)")
    print(f"  {'slot':<22} {'candidates':>10} {'read':>5} {'dropped':>8}  multi")
    dropped_total = 0
    for slot in sorted(by_slot):
        candidates = by_slot[slot]
        kept = len(routed.get(slot, []))
        dropped = candidates - kept
        dropped_total += dropped
        multi = "yes" if slot in MULTI_DOC_SLOTS else ""
        print(f"  {slot:<22} {candidates:>10} {kept:>5} {dropped:>8}  {multi}")

    never_read = unclassified + sum(no_extractor.values()) + dropped_total
    print(f"\n  documents actually read        {read:>5}  ({read / len(files):.0%})")
    print(f"  never read by anything         {never_read:>5}  ({never_read / len(files):.0%})")

    if not args.pages:
        print("\n(run with --pages to measure page capping; it parses, which takes a while)")
        return 0

    # --- stage 3: how many relevant pages are actually sent? -------------------
    from creditpaper.processing.extractors import EXTRACTORS_BY_MIME
    from creditpaper.processing.page_selector import _PATTERNS, MAX_PAGES_PER_PASS, select_pages

    extractor = EXTRACTORS_BY_MIME.get("application/pdf")
    if extractor is None:
        print("no PDF extractor registered", file=sys.stderr)
        return 1

    print(f"\nstage 3 — page selection (cap {MAX_PAGES_PER_PASS} per pass)")
    print(f"  {'slot':<22} {'pages':>7} {'matched':>8} {'sent':>6} {'unsent':>7}")
    total_matched = total_sent = 0
    for slot, paths in sorted(routed.items()):
        if slot not in _PATTERNS:
            continue
        for path in paths:
            try:
                result = extractor(path.read_bytes(), original_filename=path.name)
            except Exception:  # noqa: BLE001 - a bad file is not this report's subject
                continue
            page_texts = result.page_texts or []
            if not page_texts:
                continue
            sent = len(select_pages(page_texts, slot))
            matched = sum(
                1
                for pt in page_texts
                if any(kw in str(pt.get("text", "")).casefold() for kw in _PATTERNS[slot])
            )
            total_matched += matched
            total_sent += sent
            print(f"  {slot:<22} {len(page_texts):>7} {matched:>8} {sent:>6} {max(0, matched - sent):>7}")
    if total_matched:
        print(
            f"\n  relevant pages sent to the model  {total_sent} of {total_matched} "
            f"({total_sent / total_matched:.0%})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
