"""Group the documents no slot claims, so the taxonomy can be extended from evidence.

A real 144-document deal folder matched 37 documents to 13 of the 46 taxonomy
slots. The other 107 reached nothing. The question that decides the ingestion
architecture is what those 107 ACTUALLY ARE: canonical records we have no slot
for, or material that genuinely carries no credit facts.

Nobody can answer that without looking, and the person who should look is the
one who knows the deal. So this does the tedious part — pull each unmatched
document's opening lines, group documents whose openings resemble each other,
and write the groups to a local file — and leaves the naming to a human.

WHAT GOES WHERE. The full detail is written to a file under `output/`, which is
gitignored, and stays on this machine. Only COUNTS are printed to stdout, so the
command can be run in a shared session without putting deal contents in it. The
file is the working document; the terminal is the summary.

Usage:
    python scripts/ingest_unmatched_survey.py --folder "<deal folder>"
    # then read output/unmatched_documents.txt and name the slots
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SUPPORTED_SUFFIXES = {".pdf"}
HEAD_LINES = 6
SIMILARITY = 0.45
_SHINGLE = 3


def _shingles(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    if len(words) < _SHINGLE:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + _SHINGLE]) for i in range(len(words) - _SHINGLE + 1)}


def _similar(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _opening(path: Path) -> str:
    """The first few non-empty lines of page one — where a document names itself."""
    from creditpaper.ingest.reader import PdfiumReader

    try:
        with PdfiumReader(path) as doc:
            if doc.page_count() == 0:
                return ""
            lines = [ln.strip() for ln in doc.page(1).text.splitlines() if ln.strip()]
    except Exception:
        return ""
    return " | ".join(lines[:HEAD_LINES])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, help="deal documents folder")
    parser.add_argument("--out", default="", help="output file (default: output/unmatched_documents.txt)")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"not a folder: {folder}", file=sys.stderr)
        return 2

    from creditpaper.deals.services import slot_matcher
    from creditpaper.deals.services.slot_taxonomy import slot_type_from_row, slots_for
    from creditpaper.deals.store import slot_store
    from creditpaper.paths import ensure_output_dir

    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
    if not files:
        print("no PDFs in that folder")
        return 1

    slots = slots_for("construction", [slot_type_from_row(r) for r in slot_store.slot_types()])
    table = slot_matcher.keyword_table()

    matched: Counter[str] = Counter()
    unmatched: list[Path] = []
    for path in files:
        key = slot_matcher.match_file([path.name], None, slots, table).slot_key
        if key is None:
            unmatched.append(path)
        else:
            matched[key] += 1

    print(f"{len(files)} documents: {sum(matched.values())} matched, {len(unmatched)} unmatched")
    print("reading the opening lines of each unmatched document...")

    entries: list[tuple[Path, str, set[str]]] = []
    no_text = 0
    for path in unmatched:
        opening = _opening(path)
        if not opening:
            no_text += 1
        entries.append((path, opening, _shingles(opening)))

    # Single-link grouping on opening-line similarity. Crude on purpose: the
    # goal is to save a human from reading 107 openings, not to classify them.
    groups: list[list[int]] = []
    assigned: set[int] = set()
    for i, (_, _, shi) in enumerate(entries):
        if i in assigned:
            continue
        group = [i]
        assigned.add(i)
        for j in range(i + 1, len(entries)):
            if j in assigned:
                continue
            if _similar(shi, entries[j][2]) >= SIMILARITY:
                group.append(j)
                assigned.add(j)
        groups.append(group)
    groups.sort(key=len, reverse=True)

    out_path = Path(args.out) if args.out else ensure_output_dir() / "unmatched_documents.txt"
    with out_path.open("w", encoding="utf-8") as fh:
        fh.write("Documents no taxonomy slot claims, grouped by how their first page opens.\n")
        fh.write("For each group, either name an existing slot or propose a new one.\n")
        fh.write("A group that carries no credit facts is a valid answer: write NONE.\n")
        fh.write(f"\n{len(unmatched)} unmatched of {len(files)}; {no_text} had no text layer.\n")
        fh.write("=" * 78 + "\n")
        for n, group in enumerate(groups, 1):
            fh.write(f"\n--- group {n}: {len(group)} document(s)  ->  SLOT: ______________\n")
            for idx in group:
                path, opening, _ = entries[idx]
                fh.write(f"  file    : {path.name}\n")
                fh.write(f"  opens   : {opening[:300] or '(no text layer)'}\n")

    print(f"\n  groups found            {len(groups):>4}")
    print(f"  singletons              {sum(1 for g in groups if len(g) == 1):>4}")
    print(f"  largest group           {len(groups[0]) if groups else 0:>4} documents")
    print(f"  no text layer           {no_text:>4}")
    print(f"\nwritten to {out_path}")
    print("That file stays on this machine. Read it, name the slots, and only the")
    print("slot names need to leave it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
