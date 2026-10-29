"""Run a deal folder and report the CONFIDENCE distribution. No ground truth needed.

Confidence stopped being a constant on 2026-09-21 — it is derived from whether
the model's quote anchors to a page and whether the value it reported sits
inside that quote. This measures what that does to real documents: if most
facts flag, the review queue becomes the bottleneck and the thresholds need
evidence rather than adjustment-until-green.

TWO THINGS THE FIRST VERSION OF THIS SCRIPT GOT WRONG, both fixed here:

1. It reported 100% unscored, which measured nothing at all. Verification needs
   PAGE TEXT to anchor a quote against, and `harness_run.run_deal_folder` calls
   `ingest()` with no `page_texts_map` — so extractors ran without a
   `page_text_context` and `_scored` returned None for every fact. This parses
   each document first and passes the page text through, which is what
   production does (`run_single_document_extraction`, `ingest/document.py`).

2. It printed field paths as though they were schema. They are not: a keyed
   collection puts an extracted VALUE in the path —
   `its.guarantors[<party name slug>]` — so the paths leaked exactly the
   identity this script exists to keep out. Collection keys are now redacted
   before anything is printed.

WHAT THIS PRINTS: counts, confidence bands, and field paths with every
collection key redacted. No document text, no filenames, no party names, no
extracted values.

Usage:
    python scripts/ingest_confidence.py --folder "$CREDITPAPER_DEAL_DOCS" --show-paths
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from creditpaper.config import EXTRACTION_CONFIDENCE_THRESHOLD  # noqa: E402
from creditpaper.extractors._doc import verification_sink  # noqa: E402
from creditpaper.ingest.values import numeric_tokens  # noqa: E402
from creditpaper.ledger.service import facts_in  # noqa: E402
from creditpaper.model.core import FactStatus  # noqa: E402

SUPPORTED_SUFFIXES = {".pdf"}
DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"

# A keyed collection carries an extracted value in the path:
# `its.guarantors[acme_holdings_pty_ltd]` names a party, and printing it would
# leak the identity this script is careful about everywhere else.
_KEY = re.compile(r"\[[^\]]*\]")

_BANDS = (
    (0.95, "0.95+  verified, exact anchor"),
    (0.85, "0.85   verified, cited page differed"),
    (0.50, "0.50+  weak"),
    (0.30, "0.30+  flagged: value not in the quote"),
    (0.20, "0.20+  flagged: quote did not anchor"),
    (0.00, "below"),
)


def safe_path(path: str) -> str:
    """The field path with every collection key redacted."""
    return _KEY.sub("[...]", path)


def _band(confidence: float) -> str:
    for floor, label in _BANDS:
        if confidence >= floor:
            return label
    return "below"


# --- span ambiguity -------------------------------------------------------
# Tier 1 of the deterministic-extraction design derives a fact's value from the
# span its quote anchored to, instead of trusting the value the model typed.
# That only works when the span holds ONE candidate of the expected type.
# `value_in_span` accepts a value found anywhere in the span, so on
# "Contract Sum: $12,450,000 payable in 24 instalments over 18 months" it
# verifies 24 and 18 as readily as the contract sum. This measures how often
# that ambiguity is actually present.
_ISO_TOKEN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Two ways to count the candidates a span offers, reported side by side because
# the gap between them IS the argument for field typing.
#
#   UNTYPED — every number is interchangeable. This is what the pipeline knows
#             today, since no field declares whether it holds money, a count or
#             a term.
#   TYPED   — money, percent and plain numbers are different things. On
#             "Contract Sum: $12,450,000 payable in 24 instalments over 18
#             months" the untyped count is 3 and the typed count is 1, because
#             only one token wears a dollar sign.
#
# Decoration is a proxy for the real field typing (a `contract_sum` field
# declaring itself money). It is a floor: a typed field also distinguishes
# "24 instalments" from "18 months", which decoration cannot.
_MONEY = re.compile(r"\$\s*(\d[\d,\s]*(?:\.\d+)?)")
_PERCENT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*%")


def _canon(raw: str) -> str:
    cleaned = raw.replace(",", "").replace(" ", "")
    if cleaned.endswith(".0"):
        cleaned = cleaned[:-2]
    if "." in cleaned:
        cleaned = cleaned.rstrip("0").rstrip(".")
    return cleaned


def _classes_in(span: str) -> dict[str, str]:
    """{canonical token -> class} for every number the span holds."""
    out: dict[str, str] = {}
    for token in numeric_tokens(span):
        out[token] = "date" if _ISO_TOKEN.match(token) else "plain"
    for m in _MONEY.finditer(span):
        out[_canon(m.group(1))] = "money"
    for m in _PERCENT.finditer(span):
        out[_canon(m.group(1))] = "percent"
    return out


def _single_token(value: object) -> str | None:
    """The one token a fact's value is, or None if it is not exactly one.

    A string or boolean value ("current and in force") has no token to derive,
    so it can never come from the span this way — Tier 2 territory.
    """
    tokens = numeric_tokens(str(value))
    return tokens[0] if len(tokens) == 1 else None


def _counts(span: str, token: str) -> tuple[int, int, str]:
    """(untyped candidates, typed candidates, the value's class)."""
    classes = _classes_in(span)
    mine = classes.get(token, "plain")
    if mine == "date":
        untyped = sum(1 for c in classes.values() if c == "date")
        return untyped, untyped, "date"
    # Untyped: every non-date number competes.
    untyped = sum(1 for c in classes.values() if c != "date")
    typed = sum(1 for c in classes.values() if c == mine)
    return untyped, typed, mine


def _bucket(n: int) -> str:
    if n <= 1:
        return "1    derivable — no choice to make"
    if n <= 3:
        return "2-3  selection — low ambiguity"
    if n <= 9:
        return "4-9  selection — picking from a crowd"
    return "10+  selection — the span is a table, not a row"


_BUCKET_ORDER = (
    "1    derivable — no choice to make",
    "2-3  selection — low ambiguity",
    "4-9  selection — picking from a crowd",
    "10+  selection — the span is a table, not a row",
)


def _span_report(sink: dict, facts: dict, total_facts: int) -> None:
    anchored = 0
    not_single_token = 0
    untyped_buckets: Counter[str] = Counter()
    typed_buckets: Counter[str] = Counter()
    by_class: Counter[str] = Counter()

    for path, verification in sink.items():
        matched = verification.anchor.matched_text
        if not matched:
            continue
        anchored += 1
        fact = facts.get(path)
        if fact is None:
            continue
        token = _single_token(fact.value)
        if token is None:
            not_single_token += 1
            continue
        untyped, typed, cls = _counts(matched, token)
        by_class[cls] += 1
        untyped_buckets[_bucket(untyped)] += 1
        typed_buckets[_bucket(typed)] += 1

    print(f"\nspan ambiguity  ({anchored} of {total_facts} facts anchored to a span)")
    if not anchored:
        print("  nothing anchored — no span to measure")
        return
    pool = sum(by_class.values())
    print(f"  value is a single token (derivable at all)   {pool:>5}  {pool / anchored:>6.1%}")
    text_share = not_single_token / anchored
    print(f"  value is text or compound (Tier 2 only)      {not_single_token:>5}  {text_share:>6.1%}")
    if not pool:
        return

    print(f"\n  candidates competing for the value, of those {pool}:")
    print(f"    {'':<48}  {'untyped':>9}  {'typed':>9}")
    for label in _BUCKET_ORDER:
        u, t = untyped_buckets.get(label, 0), typed_buckets.get(label, 0)
        if u or t:
            print(f"    {label:<48}  {u:>4} {u / pool:>4.0%}  {t:>4} {t / pool:>4.0%}")

    u_one = untyped_buckets.get(_BUCKET_ORDER[0], 0)
    t_one = typed_buckets.get(_BUCKET_ORDER[0], 0)
    print(
        f"\n  Tier 1 DERIVES {u_one}/{pool} ({u_one / pool:.0%}) untyped, "
        f"{t_one}/{pool} ({t_one / pool:.0%}) typed."
    )
    print(
        f"  As a share of ALL {total_facts} facts: {u_one / total_facts:.1%} untyped, "
        f"{t_one / total_facts:.1%} typed."
    )
    print(f"  value classes: {dict(by_class)}")


def _page_texts(files: list[Path]) -> dict[Path, list[dict]]:
    """Parse each document to page text, as the upload pipeline does.

    Without this the extractors get no page text, nothing can be anchored, and
    every fact comes back unscored — a measurement of the harness rather than
    of the documents.
    """
    from creditpaper.processing.extractors import EXTRACTORS_BY_MIME

    extractor = EXTRACTORS_BY_MIME.get("application/pdf")
    if extractor is None:
        raise RuntimeError("no PDF extractor registered")

    out: dict[Path, list[dict]] = {}
    for path in files:
        try:
            result = extractor(path.read_bytes(), original_filename=path.name)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
            print(f"  ! could not parse a document ({type(exc).__name__})", file=sys.stderr)
            continue
        if result.page_texts:
            out[path] = result.page_texts
    return out


def main() -> int:
    # Every script that makes an LLM call loads .env at the top of main()
    # (config.py:11 states the convention). This one did not, so every
    # extraction call failed authentication and the script blamed the
    # documents: "no facts extracted — nothing to measure".
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, help="deal documents folder")
    parser.add_argument("--deal-id", default="confidence-probe")
    parser.add_argument("--show-paths", action="store_true", help="list field paths, keys redacted")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"not a folder: {folder}", file=sys.stderr)
        return 2

    from creditpaper.deals.utils.ingest import ingest
    from creditpaper.harness.route import route_deal
    from creditpaper.model import DealModel

    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
    if not files:
        print("no PDFs in that folder")
        return 1

    print(f"parsing {len(files)} documents...")
    page_texts_map = _page_texts(files)
    print(f"parsed {len(page_texts_map)} of {len(files)}; extracting...")
    if not page_texts_map:
        print("nothing parsed, so nothing can be verified — stopping", file=sys.stderr)
        return 1

    model = DealModel(tenant_id=DEFAULT_TENANT, deal_id=args.deal_id)
    started = time.monotonic()
    with verification_sink() as sink:
        report = ingest(model, route_deal([str(p) for p in files]), page_texts_map=page_texts_map)
    elapsed = time.monotonic() - started

    # A failed EXTRACTION and an empty DOCUMENT look identical in the fact
    # count, and conflating them is how a broken harness gets read as a
    # measurement. Report the harness failure first, in its own words.
    if report.failed_documents:
        print(
            f"\n{report.failed_documents} document(s) FAILED to extract. "
            f"That is a harness failure, not a result — the numbers below "
            f"(if any) describe only what survived.",
            file=sys.stderr,
        )
        if report.cost_usd == 0.0:
            print(
                "Cost was $0.00, so no LLM call succeeded at all. Check "
                "ANTHROPIC_API_KEY in .env before reading anything into this run.",
                file=sys.stderr,
            )
            return 1

    facts = facts_in(model)
    if not facts:
        print("no facts extracted — nothing to measure")
        return 1

    by_status: Counter[str] = Counter()
    by_band: Counter[str] = Counter()
    unscored: list[str] = []
    flagged: list[str] = []

    for path, fact in sorted(facts.items()):
        by_status[fact.status.value] += 1
        if fact.status is FactStatus.FLAGGED:
            flagged.append(safe_path(path))
        if fact.confidence is None:
            # Unscored is NOT low confidence: nothing could be checked. Either
            # the model returned no quote, or one too short to verify.
            unscored.append(safe_path(path))
            continue
        by_band[_band(float(fact.confidence))] += 1

    total = len(facts)
    below = sum(
        1
        for f in facts.values()
        if f.confidence is not None and float(f.confidence) < EXTRACTION_CONFIDENCE_THRESHOLD
    )

    print(f"\n{total} facts  ({elapsed:.0f}s, ${report.cost_usd:.2f})\n")
    print("by status")
    for status, count in sorted(by_status.items()):
        print(f"  {status:<12} {count:>5}  {count / total:>6.1%}")

    print("\nby confidence")
    for _, label in _BANDS:
        count = by_band.get(label, 0)
        if count:
            print(f"  {label:<42} {count:>5}  {count / total:>6.1%}")
    if unscored:
        share = len(unscored) / total
        print(f"  {'unscored (nothing checkable)':<42} {len(unscored):>5}  {share:>6.1%}")

    print(
        f"\nwould reach review: {below + len(unscored)} of {total} "
        f"({(below + len(unscored)) / total:.1%})  [threshold {EXTRACTION_CONFIDENCE_THRESHOLD}]"
    )

    _span_report(sink, facts, total)

    if args.show_paths:
        # Counted, not listed one by one: a repeated path says more than a long
        # list, and fewer lines is less to scan for anything that should not be
        # here.
        if flagged:
            print("\nflagged field paths (collection keys redacted):")
            for path, count in sorted(Counter(flagged).items()):
                print(f"  {count:>3}x  {path}")
        if unscored:
            print("\nunscored field paths (collection keys redacted):")
            for path, count in sorted(Counter(unscored).items()):
                print(f"  {count:>3}x  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
