"""Does a cheap model do schema-directed extraction with correct field paths?

The general-extraction design (docs/specs/2026-09-22-general-extraction.md)
prices a whole deal at ~$2.00 by running Haiku 4.5 over every page with the
field reference as a cached prefix. Its own adversarial review named the
assumption underneath that number:

    "The budget rests on an untested assumption about Haiku. If its path
    accuracy is poor, the escalation tier is not 5% but 40%, and the cost is
    ~$3.50, not ~$2.00."

This runs that experiment. No document type is named in the prompt: the model
gets a window of pages and the list of every field path in the deal model, and
has to decide what it is looking at.

THREE MEASUREMENTS, in order of how much they decide:

1. PATH VALIDITY — is the returned path one we actually offered? The field
   reference is the allowlist, so this is exact and needs no ground truth. An
   invented path is a fact that cannot be applied at all.

2. SUBTREE ACCURACY — the error class general extraction CREATES. A
   per-document extractor cannot write outside its own subtree; this can put a
   cost-report figure into an ITS field, and verification will pass it at 0.95
   because the value really is in the quote. Measured by running windows from
   documents the existing router classifies, and checking the facts land in
   the `model_block` the taxonomy says that slot feeds.

3. ANCHOR RATE — do the clauses locate on the page, through the existing
   deterministic verifier.

Plus the two numbers the budget is made of: actual cost and actual latency.

WHAT THIS PRINTS: counts, rates, dollars, seconds, and field-path PREFIXES with
collection keys redacted. No document text, no filenames, no values.

`--dump` is the ONE exception and it prints nothing: it writes a JSON record
per fact, INCLUDING the value and the span text, to a path you name. That file
is tenant data under the same rule as `fixtures/` - write it outside the repo
and never commit it. It exists so two verifiers can be compared on identical
draws (see the 2026-09-23 comparison in the general-extraction spec).

Usage:
    python scripts/general_extraction_probe.py --folder "$CREDITPAPER_DEAL_DOCS"
    python scripts/general_extraction_probe.py --folder ... --max-windows 20
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from creditpaper.ingest import consensus, fact_cache  # noqa: E402
from creditpaper.ingest.commit import default_checker_factory  # noqa: E402
from creditpaper.ingest.corpus import Page, clause_at, plan_corpus  # noqa: E402
from creditpaper.ingest.harvest import harvest_prompt  # noqa: E402
from creditpaper.model.concepts import concept_for  # noqa: E402
from creditpaper.paths import output_dir  # noqa: E402

SUPPORTED_SUFFIXES = {".pdf"}
FIELD_REFERENCE = ROOT / "docs" / "FIELD_REFERENCE.generated.md"

# Prefiltering, windowing and dedupe all live in `ingest/corpus.py`. This
# script carried its own copy until 2026-09-22 and it drifted the moment the
# module gained window-level dedupe.

_KEY_IN_PATH = re.compile(r"\[[^\]]*\]")
_REF_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|")


def redact(path: str) -> str:
    return _KEY_IN_PATH.sub("[...]", path)


def allowed_paths() -> set[str]:
    """Every path the field reference offers, keys normalised to `[...]`."""
    out: set[str] = set()
    for line in FIELD_REFERENCE.read_text(encoding="utf-8").splitlines():
        m = _REF_ROW.match(line.strip())
        if m:
            out.add(redact(m.group(1)))
    return out


_BOOLEAN = {"true", "false"}


def _value_is_checkable(value: object) -> bool:
    """Whether `value_in_span` can possibly apply — production's note/fact split.

    `_doc.add_note_fact` verifies a JUDGEMENT or SUMMARY on its ANCHOR ALONE,
    because its value cannot appear in its own quote by construction: "true" is
    the model's reading of "the licence is current and in force", and a summary
    is a paraphrase of the sentence it came from.

    An earlier version of this probe checked the value on EVERY fact, which is
    not what production does, so it counted those as FLAGGED_VALUE and
    overstated the failure rate. Same rule as
    `tests/test_prompt_anchor_contract.py::_value_is_checkable`.
    """
    text = str(value).strip()
    if text.casefold() in _BOOLEAN:
        return False
    return len(text.split()) <= 5


def _pages(path: Path) -> list[tuple[int, str]]:
    from creditpaper.ingest.reader import PdfiumReader

    try:
        with PdfiumReader(path) as doc:
            return [(n, doc.page(n).text) for n in range(1, doc.page_count() + 1)]
    except Exception:
        return []


_PARSE_TENANT = "00000000-0000-0000-0000-000000000001"


def _parse_one(path: Path) -> tuple[Path, list[dict]]:
    """One document through the PRODUCTION parse, via the production cache.

    `extract_pdf`, not `page.get_text()`: it does the OCR fallback, and OCR is
    where 16-24% of this corpus lives. Anything measured without it is measured
    on a different corpus than production reads.
    """
    import hashlib
    import json

    from creditpaper.processing.extractors.pdf import extract_pdf

    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()

    # NOT `ingest/parse_cache`: it stores `{page: text}` and has nowhere to put
    # `ocr_used`, so a cached read silently dropped the flag and every fact came
    # back looking as though it stood on a real text layer. Production does not
    # have that problem — it reads `deal_document_extractions.page_texts`, which
    # is the whole `PageText` dict — but a measurement that loses the flag
    # measures the wrong pipeline, which has now happened three times.
    #
    # TENANT DATA: page text, under `output_dir()`, which is gitignored.
    cache_dir = output_dir() / "probe_parse"
    cache_file = cache_dir / f"{digest}.json"
    if cache_file.is_file():
        try:
            return path, json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    result = extract_pdf(raw, original_filename=path.name)
    pages = [
        {
            "page": int(pt["page"]),
            "text": str(pt["text"]),
            "ocr_used": bool(pt.get("ocr_used")),
        }
        for pt in result.page_texts
    ]
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(pages), encoding="utf-8")
    except OSError:
        pass  # caching is an optimisation, never a gate
    return path, pages


def parse_corpus(files: list[Path], workers: int = 8) -> list[Page]:
    """Every page of every document, parsed IN PARALLEL through production.

    Serial parsing was 19 minutes on this corpus once OCR started running. The
    pool is separate from the extraction pool because this is CPU-bound
    (Tesseract) while that one waits on a network.
    """
    from creditpaper.concurrency import pool

    out: list[Page] = []
    with pool(workers, thread_name_prefix="probe-parse") as ex:
        for path, entries in ex.map(_parse_one, files):
            out.extend(
                Page(
                    document_id=str(path),
                    page=int(e["page"]),
                    text=str(e["text"]),
                    ocr_used=bool(e.get("ocr_used")),
                )
                for e in entries
                if int(e.get("page", 0) or 0) > 0
            )
    return out


def harvest(system: Any, window_text: str, model: str, max_tokens: int = 8000) -> tuple[list[dict], object]:
    """One window to the model, through the ENGINE's harvester.

    The call itself moved to `ingest/harvest.py` on 2026-09-23. It was defined
    here, which meant every number this probe produced was measured against
    code production would never run - the corpus path had no harvester in
    `src/` at all. Now the two cannot drift: this is the shipped call.
    """
    from creditpaper.ingest.harvest import harvest_window

    facts = harvest_window(
        system, window_text, model_id=model, max_tokens=max_tokens, tool="probe:general_extraction"
    )
    return facts, None


def main() -> int:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True)
    parser.add_argument(
        "--dump",
        default="",
        help="write one JSON record per fact (verdict, value, span) for offline comparison",
    )
    parser.add_argument("--window", type=int, default=10)
    parser.add_argument("--max-windows", type=int, default=0, help="0 = all")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=8000,
        help="on Sonnet 5 THINKING SHARES this budget, so a window yielding many facts "
        "truncates at 8000 and the guard in llm/client.py raises rather than "
        "returning half a draw",
    )
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument(
        "--parse-workers",
        type=int,
        default=8,
        help="parallel document parses; OCR is CPU-bound so this is not the LLM pool",
    )
    parser.add_argument("--tenant", default="00000000-0000-0000-0000-000000000001")
    parser.add_argument("--deal-id", default="general-extraction-probe")
    parser.add_argument(
        "--passes",
        type=int,
        default=1,
        help="read each window N times and union the draws; pass count is a confidence signal",
    )
    parser.add_argument(
        "--line-anchored",
        action="store_true",
        help="model returns a LINE number; we cut the clause from page text",
    )
    parser.add_argument(
        "--support",
        action="store_true",
        help="run the semantic support check on out-of-subtree facts (spends per fact)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="always call the model; measures the process, not the replay",
    )
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"not a folder: {folder}", file=sys.stderr)
        return 2
    if not FIELD_REFERENCE.exists():
        print(f"missing {FIELD_REFERENCE}; run scripts/gen_field_reference.py", file=sys.stderr)
        return 2

    from creditpaper.concurrency import pool
    from creditpaper.harness.route import classify
    from creditpaper.obs.ledger import cost_session

    reference = FIELD_REFERENCE.read_text(encoding="utf-8")
    # The shipped prompts, so a probed run takes the route production takes.
    system = harvest_prompt(reference, line_anchored=args.line_anchored)
    allow = allowed_paths()
    print(f"field list: {len(allow)} paths, system prefix ~{len(system.text) // 4:,} tokens")

    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
    print(f"parsing {len(files)} documents...")

    # Planning is `ingest/corpus.py`, not a private copy here. This script used
    # to carry its own prefilter and windowing, which drifted from the module
    # the moment the module gained window-level dedupe.
    slot_by_document: dict[str, str | None] = {str(path): classify(path) for path in files}
    # The PRODUCTION parse, in parallel. `_pages` was a bare text read with no
    # OCR, so every number this script produced before 2026-09-25 was taken on
    # a corpus missing 16-24% of its pages.
    parse_started = time.monotonic()
    corpus_pages = parse_corpus(files, workers=args.parse_workers)
    parse_s = time.monotonic() - parse_started
    ocr_pages = sum(1 for p in corpus_pages if p.ocr_used)
    print(
        f"parsed in {parse_s:.0f}s  ({len(corpus_pages)} pages, {ocr_pages} read by OCR, "
        f"{args.parse_workers} workers)"
    )

    plan = plan_corpus(corpus_pages, window=args.window)
    print(
        f"corpus: {plan.pages_total} pages, {plan.pages_empty} empty, "
        f"{plan.pages_dropped} structural, {plan.pages_rescued} rescued, "
        f"{plan.pages_read} read"
    )
    print(f"  {plan.calls} calls ({plan.windows_deduped} saved by reading duplicates once)")

    # Slot comes from the EXISTING router and is weak ground truth for subtree
    # accuracy only - it never reaches the prompt.
    windows = [
        (
            slot_by_document.get(w.document_id),
            w.texts_by_page(),
            w.numbered() if args.line_anchored else w.text,
            w.content_hash,
            w,
        )
        for w in plan.windows
    ]
    if args.max_windows:
        windows = windows[: args.max_windows]
    print(f"{len(windows)} windows of up to {args.window} pages; extracting on {args.model}...")

    # The cache is what makes a re-ingestion identical rather than merely
    # similar. Hit = the stored draw, no model call. `--no-cache` measures the
    # underlying process instead of the replay, which is what the determinism
    # gate needs.
    hits = 0

    def read(window) -> tuple[list[dict], object]:
        nonlocal hits
        _slot, _pages, body, digest, _w = window
        if not args.no_cache:
            stored = fact_cache.get(args.tenant, args.deal_id, digest, model_id=args.model)
            if stored is not None:
                hits += 1
                return stored, None
        if args.passes > 1:
            # Passes are UNIONED, not intersected: a fact one pass found is a
            # fact that was found — the others did not look at it rather than
            # disagree with it. The MERGED result is what gets cached, so the N
            # passes are paid for once and replayed forever.
            draws = [harvest(system, body, args.model)[0] for _ in range(args.passes)]
            merged = consensus.merge(draws)
            facts = [{"path": f.path, "value": f.value, "line": f.line, "seen_in": f.seen_in} for f in merged]
            resp = None
        else:
            facts, resp = harvest(system, body, args.model, args.max_tokens)
        if not args.no_cache:
            fact_cache.put(args.tenant, args.deal_id, digest, facts, model_id=args.model)
        return facts, resp

    started = time.monotonic()
    with cost_session("general-extraction-probe") as ledger:
        with pool(args.workers) as ex:
            results = list(ex.map(read, windows))
    elapsed = time.monotonic() - started
    if not args.no_cache:
        print(f"  cache: {hits} hit, {len(windows) - hits} extracted")

    from creditpaper.ingest.verify import Verdict, verify_fact, verify_located

    # routing key -> the model blocks the taxonomy says that slot feeds.
    #
    # This needs the database, and measurement 2 is the only thing that uses
    # it. An earlier version let a missing DATABASE_URL raise here, AFTER the
    # LLM calls had been paid for — throwing away the run to report a number
    # that two of the three measurements do not need.
    blocks_for_route: dict[str, set[str]] = {}
    try:
        from creditpaper.deals.services.slot_matcher import routing_keys_for
        from creditpaper.deals.services.slot_taxonomy import slot_type_from_row
        from creditpaper.deals.store import slot_store

        for row in slot_store.slot_types():
            slot = slot_type_from_row(row)
            for rk in routing_keys_for(slot.key):
                blocks_for_route.setdefault(rk, set()).update(slot.blocks())
    except Exception as exc:  # noqa: BLE001 - the taxonomy is optional here
        print(
            f"\n! no taxonomy ({type(exc).__name__}): subtree accuracy will be "
            f"skipped. Set DATABASE_URL to measure it.",
            file=sys.stderr,
        )

    total = invalid = bad_lines = 0
    # How many passes saw each fact — the confidence signal multi-pass buys.
    seen_counts: list[int] = []
    anchored = 0
    verdicts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    roots: Counter[str] = Counter()
    invalid_examples: Counter[str] = Counter()
    in_subtree = out_subtree = 0
    # WHICH slot sent a fact WHERE. A real misassignment and a too-narrow
    # expectation map produce the same RATE and completely different tables.
    misplaced: Counter[tuple[str, str]] = Counter()
    support_counts: Counter[str] = Counter()
    support_why: Counter[str] = Counter()
    judged_windows = 0

    dump: list[dict] | None = [] if args.dump else None
    for wi, ((slot, pages, _body, _digest, window), (facts, _resp)) in enumerate(
        zip(windows, results, strict=True)
    ):
        expected = blocks_for_route.get(slot or "", set())
        if expected:
            judged_windows += 1
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            raw_path = str(fact.get("path") or "")
            if not raw_path:
                continue
            total += 1
            if isinstance(fact.get("seen_in"), int):
                seen_counts.append(int(fact["seen_in"]))
            norm = redact(raw_path)
            root = norm.split(".")[0].split("[")[0]
            roots[root] += 1
            if norm not in allow:
                invalid += 1
                invalid_examples[norm] += 1
                continue
            if expected:
                if root in expected:
                    in_subtree += 1
                else:
                    out_subtree += 1
                    misplaced[(slot or "?", root)] += 1
            if args.line_anchored:
                # The model pointed; we cut. `clause_at` builds the clause from
                # the page text we already hold, so it anchors by construction
                # and always meets the word minimum — TOO_SHORT and UNALIGNED
                # are unreachable. A line outside the window is the one way
                # this can fail, and it is counted rather than guessed at.
                raw_line = fact.get("line")
                if not isinstance(raw_line, int):
                    bad_lines += 1
                    continue
                located = clause_at(window, raw_line)
                if located is None:
                    bad_lines += 1
                    continue
                # The semantic leg, on the facts the subtree signal triages:
                # a value can be in its span and not stated OF THAT FIELD, and
                # no deterministic leg can tell the difference.
                checker = None
                if args.support and expected and root not in expected:
                    concept = concept_for(raw_path)
                    if concept is not None:
                        checker = default_checker_factory(raw_path, concept)
                v = verify_located(
                    value=fact.get("value"),
                    page_text=pages[located.page],
                    page=located.page,
                    span=located.span,
                    check_value=_value_is_checkable(fact.get("value")),
                    support_checker=checker,
                    material=checker is not None,
                    # Production caps a fact read off OCR'd text; so does this,
                    # or the probe measures a pipeline that no longer exists.
                    ocr_text=located.page in window.ocr_pages,
                )
                if v.support is not None:
                    support_counts[v.support.supports] += 1
                    if v.support.supports != "yes":
                        support_why[str(v.support.reason_code or "-")] += 1
                verdicts[v.verdict.value] += 1
                if dump is not None:
                    dump.append(
                        {
                            "w": wi,
                            "path": norm,
                            "line": raw_line,
                            "verdict": v.verdict.value,
                            "value": fact.get("value"),
                            "span": list(located.span),
                            "page": located.page,
                            "text": pages[located.page][located.span[0] : located.span[1]],
                        }
                    )
                for r in getattr(v, "reasons", ()) or ():
                    reasons[getattr(r, "value", str(r))] += 1
                anchored += 1
                continue
            else:
                clause = str(fact.get("clause") or "")
                if not clause:
                    continue
                page = fact.get("page")
            v = verify_fact(
                value=fact.get("value"),
                quote=clause,
                pages=pages,
                cited_page=int(page) if isinstance(page, int) else None,
                check_value=_value_is_checkable(fact.get("value")),
            )
            verdicts[v.verdict.value] += 1
            for r in getattr(v, "reasons", ()) or ():
                reasons[getattr(r, "value", str(r))] += 1
            if v.verdict is not Verdict.REFUSED_SHORT_QUOTE and v.anchor.matched_text:
                anchored += 1

    raw = getattr(ledger, "total_usd", 0.0)
    cost = raw() if callable(raw) else float(raw)
    raw_failed = getattr(ledger, "failed_calls", 0)
    failed = raw_failed() if callable(raw_failed) else int(raw_failed)

    print(f"\n{total} facts from {len(windows)} windows  ({elapsed:.0f}s, ${cost:.2f})")
    if failed:
        print(f"  ! {failed} call(s) FAILED — a harness failure, not a result", file=sys.stderr)
    if not total:
        print("no facts returned — nothing to measure")
        return 1

    print("\n1. PATH VALIDITY  (is the path one we offered?)")
    valid = total - invalid
    print(f"  valid                    {valid:>5}  {valid / total:>6.1%}")
    print(f"  INVENTED                 {invalid:>5}  {invalid / total:>6.1%}")

    print(f"\n2. SUBTREE ACCURACY  ({judged_windows} windows had a routed slot)")
    judged = in_subtree + out_subtree
    if judged:
        print(f"  in the expected block    {in_subtree:>5}  {in_subtree / judged:>6.1%}")
        print(f"  OUTSIDE it               {out_subtree:>5}  {out_subtree / judged:>6.1%}")
    else:
        print("  no routed windows — cannot judge (run without --max-windows)")

    if args.line_anchored:
        print(f"\n  lines cited that are not in the window: {bad_lines}")
    if args.passes > 1 and seen_counts:
        dist = Counter(seen_counts)
        print(f"\n  agreement across {args.passes} passes:")
        for count in sorted(dist, reverse=True):
            n = dist[count]
            print(f"    seen in {count}/{args.passes}   {n:>5}  {n / len(seen_counts):>6.1%}")
    print("\n3. ANCHOR RATE  (existing deterministic verifier)")
    checked = sum(verdicts.values())
    if checked:
        print(f"  anchored to a span       {anchored:>5}  {anchored / checked:>6.1%}")
        for verdict, count in verdicts.most_common():
            print(f"    {verdict:<24} {count:>5}  {count / checked:>6.1%}")
        if reasons:
            print("\n  reasons given (a verdict may carry several):")
            for reason, count in reasons.most_common():
                print(f"    {reason:<24} {count:>5}")

    if support_counts:
        total_checked = sum(support_counts.values())
        # NOT in the cost line above: `cost_session` closes around the harvest
        # loop, and verification runs after it. Saying so is better than
        # printing a total that silently excludes a per-fact model call.
        print(f"\n4. SEMANTIC SUPPORT  ({total_checked} facts checked, cost NOT in the total above)")
        for answer, count in support_counts.most_common():
            print(f"  {answer:<12} {count:>5}  {count / total_checked:>6.1%}")
        for why, count in support_why.most_common():
            print(f"    reason {why:<20} {count:>4}")

    if misplaced:
        print("\nout-of-subtree, routed slot -> fact root (top 20):")
        for (slot_key, root), count in misplaced.most_common(20):
            print(f"  {count:>4}x  {slot_key:<28} -> {root}")

    print("\nfact roots (top 15):")
    for root, count in roots.most_common(15):
        print(f"  {root:<28} {count:>5}")

    if invalid_examples:
        print(f"\ninvented paths (top 10 of {len(invalid_examples)} distinct):")
        for path, count in invalid_examples.most_common(10):
            print(f"  {count:>3}x  {path}")

    if dump is not None:
        Path(args.dump).write_text("\n".join(json.dumps(r, default=str) for r in dump), encoding="utf-8")
        print(f"\nwrote {len(dump)} records to {args.dump}")
    per_deal = cost / len(windows) * 166 if windows else 0.0
    print(f"\nextrapolated to 166 windows (a full deal): ${per_deal:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
