"""Measure the corpus a general extraction pass would have to read, and price it.

General extraction has no per-document-type code and no routing gate: every
page is in scope. That makes cost a function of the CORPUS, not of how many
extractors happen to exist, so the corpus is the thing to measure before
designing anything.

Four numbers decide the architecture:

  pages            how much there is to read at all
  tokens           what it costs to read it once, naively
  duplicate pages  a deal folder repeats itself (the same term sheet filed in
                   three places, plan sheets reprinted across submissions).
                   Identical page text extracted twice is pure waste.
  empty/sparse     plan sheets, dividers, signature pages and scans with no
                   text layer carry no facts and can be skipped deterministically

WHAT THIS PRINTS: counts, token estimates and dollar figures. No filenames, no
page text, no values. Safe to paste.

Usage:
    python scripts/corpus_budget.py --folder "$CREDITPAPER_DEAL_DOCS"
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SUPPORTED_SUFFIXES = {".pdf"}

# Rough but stable: ~4 characters per token for English prose with numbers.
# Good enough for an order-of-magnitude budget, and it does not drift the way a
# tokeniser version does.
CHARS_PER_TOKEN = 4

# A page needs SOME substance to carry a fact. These thresholds are deliberately
# permissive -- the point is to drop dividers and plan sheets, not to judge
# relevance, which is what the reading pass is for.
MIN_CHARS = 200
MIN_WORDS = 30

_WORD = re.compile(r"[A-Za-z]{2,}")
_NUM = re.compile(r"\d")


def _norm(text: str) -> str:
    """Whitespace-normalised page text, for hashing duplicates."""
    return " ".join(text.split()).casefold()


def _page_texts(path: Path) -> list[str]:
    from creditpaper.ingest.reader import PdfiumReader

    try:
        with PdfiumReader(path) as doc:
            return [doc.page(n).text for n in range(1, doc.page_count() + 1)]
    except Exception:
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True)
    parser.add_argument("--window", type=int, default=5, help="pages per extraction call")
    parser.add_argument(
        "--schema-tokens",
        type=int,
        default=20000,
        help="size of the cached fact-schema prefix sent with every call",
    )
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"not a folder: {folder}", file=sys.stderr)
        return 2

    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
    if not files:
        print("no PDFs in that folder")
        return 1

    print(f"reading {len(files)} documents...")

    total_pages = 0
    no_text_layer = 0
    sparse = 0
    substantive_chars = 0
    seen: Counter[str] = Counter()
    unique_chars = 0
    unique_pages = 0

    for path in files:
        for text in _page_texts(path):
            total_pages += 1
            stripped = text.strip()
            if not stripped:
                no_text_layer += 1
                continue
            words = len(_WORD.findall(stripped))
            if len(stripped) < MIN_CHARS or words < MIN_WORDS:
                sparse += 1
                continue
            substantive_chars += len(stripped)
            digest = hashlib.sha256(_norm(stripped).encode("utf-8")).hexdigest()
            seen[digest] += 1
            if seen[digest] == 1:
                unique_pages += 1
                unique_chars += len(stripped)

    substantive = total_pages - no_text_layer - sparse
    duplicates = substantive - unique_pages

    print("\npages")
    print(f"  total                       {total_pages:>7}")
    print(f"  no text layer (needs OCR)   {no_text_layer:>7}  {no_text_layer / total_pages:>6.1%}")
    print(f"  sparse (divider/plan/sig)   {sparse:>7}  {sparse / total_pages:>6.1%}")
    print(f"  substantive                 {substantive:>7}  {substantive / total_pages:>6.1%}")
    print(f"  ... of which duplicates     {duplicates:>7}  {duplicates / total_pages:>6.1%}")
    print(f"  UNIQUE substantive          {unique_pages:>7}  {unique_pages / total_pages:>6.1%}")

    naive_tokens = substantive_chars // CHARS_PER_TOKEN
    unique_tokens = unique_chars // CHARS_PER_TOKEN
    print(f"\ncontent tokens (~{CHARS_PER_TOKEN} chars/token)")
    print(f"  every substantive page      {naive_tokens:>9,}")
    print(f"  unique pages only           {unique_tokens:>9,}")
    if naive_tokens:
        print(f"  saved by dedupe             {1 - unique_tokens / naive_tokens:>9.1%}")

    windows = max(1, -(-unique_pages // args.window))
    print(f"\ncalls at {args.window} pages per window: {windows}")

    # Per-million prices. Cache reads are a tenth of base input, which is the
    # whole reason the schema prefix is affordable at this call volume.
    models = (
        ("haiku 4.5", 1.00, 5.00),
        ("sonnet 5", 3.00, 15.00),
        ("opus 5", 15.00, 75.00),
    )
    out_tokens_per_call = 600

    print(f"\ncost, schema prefix {args.schema_tokens:,} tokens cached (reads at 0.1x)")
    print(f"  {'model':<12} {'content':>9} {'cache':>9} {'output':>9} {'TOTAL':>9}   {'uncached':>9}")
    for name, in_price, out_price in models:
        content = unique_tokens * in_price / 1_000_000
        cache = windows * args.schema_tokens * 0.1 * in_price / 1_000_000
        output = windows * out_tokens_per_call * out_price / 1_000_000
        uncached = windows * args.schema_tokens * in_price / 1_000_000 + content + output
        total = content + cache + output
        print(
            f"  {name:<12} {'$' + format(content, '.2f'):>9} {'$' + format(cache, '.2f'):>9} "
            f"{'$' + format(output, '.2f'):>9} {'$' + format(total, '.2f'):>9}   "
            f"{'$' + format(uncached, '.2f'):>9}"
        )

    print(f"\n  (output assumed {out_tokens_per_call} tokens/call; cache write not amortised)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
