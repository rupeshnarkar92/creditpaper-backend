"""How much variance is the fact cache freezing?

`ingest/fact_cache.py` makes a fact sheet REPEATABLE by replaying the first
draw. It does not make it stable — whichever sample landed first is the one
every later read agrees with. If the underlying extraction is 60% stable, the
cache freezes a coin-flip and presents it as settled.

That is a fair trade (a stable wrong fact can be found and corrected once; an
unstable one cannot be corrected at all) but only if the number is known. This
measures it, by running general extraction N times with the cache OFF and
feeding the runs to `harness/determinism.py`, which buckets every path:

  stable            present every run, same value, same anchor
  flapping          present in some runs, absent in others
  value_divergent   present every run with different values — the dangerous
                    one, because it looks settled in any single run
  anchor_divergent  same value, different page or clause. The figure survives;
                    the citation the paper prints does not.

`ingest/determinism_gate.py` turns that into a pass/fail at STABILITY_BAR.

WHAT THIS PRINTS: counts, rates and field-path PREFIXES with collection keys
redacted. No document text, no filenames, no values.

Usage:
    python scripts/general_extraction_determinism.py --folder "$CREDITPAPER_DEAL_DOCS" --runs 2
    python scripts/general_extraction_determinism.py --folder ... --runs 3 --max-windows 40
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

FIELD_REFERENCE = ROOT / "docs" / "FIELD_REFERENCE.generated.md"
SUPPORTED_SUFFIXES = {".pdf"}


def _facts_as_model_fields(
    raw: list[dict], window, *, line_anchored: bool, canonical_keys: bool = False
) -> dict:
    """The probe's raw dicts as `{path: Fact}`, which is what the measurer takes.

    `_anchor_key` compares page and clause, so both must be carried: a fact
    that keeps its value but moves its citation is `anchor_divergent`, and
    losing the clause here would silently reclassify those as stable.
    """
    from creditpaper.ingest.corpus import clause_at
    from creditpaper.ingest.entity_keys import canonicalise_path
    from creditpaper.ingest.general import _column_rows
    from creditpaper.model.core import ExtractionMethod, Fact, FactStatus, Provenance

    out: dict[str, Fact] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if canonical_keys and path:
            # The merge under test: two spellings of one entity become one
            # path, so a collection entry that flapped because the document
            # called it something different reads as the single fact it is.
            path = canonicalise_path(path)
        value = item.get("value")
        if not path or value is None:
            continue
        if line_anchored:
            # The anchor is ours, so a fact's identity across runs is the SPAN
            # we cut, not a sentence the model retyped. Two runs citing the
            # same line agree exactly; two runs citing neighbouring lines of
            # one clause do not, which is the signal `anchor_divergent` is for.
            raw_line = item.get("line")
            located = clause_at(window, raw_line) if isinstance(raw_line, int) else None
            if located is None:
                continue
            page, location = located.page, located.text[:300]
            # The SAME column enumeration production does. Without this the
            # measurement scores a pipeline that no longer exists: the engine
            # reads a table's whole column and this would score the one cell
            # the model happened to name. That drift is the reason the
            # harvester was moved into `src/` on 2026-09-23.
            page_text = window.texts_by_page()[located.page]
            rows = _column_rows(path, value, page_text, located.line_span)
            for row_path, cell, row in rows:
                out[row_path] = Fact(
                    value=cell,
                    status=FactStatus.EXTRACTED,
                    confidence=0.9,
                    provenance=Provenance(
                        document_id=window.document_id,
                        page=located.page,
                        location=page_text[row.start : row.end][:300],
                        method=ExtractionMethod.LLM_EXTRACTION,
                    ),
                )
            if rows:
                # The cited row is in the enumeration under a key from its own
                # label; the model's arbitrary key is what flaps.
                continue
        else:
            page = item.get("page")
            location = str(item.get("clause") or "")[:300]
        out[path] = Fact(
            value=value,
            status=FactStatus.EXTRACTED,
            confidence=0.9,
            provenance=Provenance(
                document_id=window.document_id,
                page=int(page) if isinstance(page, int) else None,
                location=location,
                method=ExtractionMethod.LLM_EXTRACTION,
            ),
        )
    return out


def _dump_value(fact: object) -> dict:
    """One fact as the plain dict a re-cut needs: value, page, clause."""
    provenance = getattr(fact, "provenance", None)
    return {
        "value": getattr(fact, "value", None),
        "page": getattr(provenance, "page", None),
        "location": (getattr(provenance, "location", "") or "")[:300],
    }


def _drawn(
    system: Any, window_text: str, model: str, max_tokens: int, passes: int, temperature: float | None = None
) -> tuple[list[dict], object]:
    """One run's answer for one window: `passes` draws, unioned.

    Through `general.merge_draws`, which is what production stores in the fact
    cache and replays on every later read. Measuring a single draw's stability
    would answer a question nobody asks.
    """
    from creditpaper.ingest.general import merge_draws
    from creditpaper.ingest.harvest import harvest_window

    draws = [
        harvest_window(
            system,
            window_text,
            model_id=model,
            max_tokens=max_tokens,
            tool="probe:general_determinism",
            temperature=temperature,
        )
        for _ in range(max(1, passes))
    ]
    return merge_draws(draws), None


def main() -> int:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--window", type=int, default=10)
    parser.add_argument("--max-windows", type=int, default=0, help="0 = all")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--parse-workers", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--line-anchored", action="store_true")
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="sampling temperature for every draw; unset sends none, as production does",
    )
    parser.add_argument(
        "--dump",
        default="",
        help="write each run's {path: value} to JSON so a measurement can be re-cut offline",
    )
    parser.add_argument(
        "--passes",
        type=int,
        default=1,
        help="draws per window per run, unioned as production does; the cache stores the MERGE",
    )
    parser.add_argument(
        "--canonical-keys",
        action="store_true",
        help="merge collection keys to a canonical form before comparing runs",
    )
    args = parser.parse_args()

    if args.runs < 2:
        print("determinism needs at least two runs", file=sys.stderr)
        return 2
    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"not a folder: {folder}", file=sys.stderr)
        return 2

    sys.path.insert(0, str(ROOT / "scripts"))
    from general_extraction_probe import parse_corpus, redact

    from creditpaper.concurrency import pool
    from creditpaper.harness.determinism import measure_determinism
    from creditpaper.ingest.corpus import plan_corpus
    from creditpaper.ingest.determinism_gate import STABILITY_BAR, stability_from
    from creditpaper.ingest.harvest import harvest_prompt
    from creditpaper.obs.ledger import cost_session

    _ref = FIELD_REFERENCE.read_text(encoding="utf-8")
    # The shipped prompts, so a measured run takes the route production takes.
    system = harvest_prompt(_ref, line_anchored=args.line_anchored)

    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
    # The PRODUCTION parse, in parallel, via the probe's shared helper: OCR is
    # where 16-24% of this corpus lives, and a stability number taken without it
    # is a number about a different corpus.
    corpus_pages = parse_corpus(files, workers=args.parse_workers)
    plan = plan_corpus(corpus_pages, window=args.window)
    windows = list(plan.windows)
    if args.max_windows:
        windows = windows[: args.max_windows]
    print(f"{len(windows)} windows, {args.runs} runs, cache BYPASSED (measuring the process)")

    runs: list[dict] = []
    runs_canon: list[dict] = []
    total_cost = 0.0
    for n in range(args.runs):
        started = time.monotonic()
        with cost_session(f"determinism-run-{n}") as ledger:
            with pool(args.workers) as ex:
                results = list(
                    ex.map(
                        lambda w: _drawn(
                            system,
                            w.numbered() if args.line_anchored else w.text,
                            args.model,
                            args.max_tokens,
                            args.passes,
                            args.temperature,
                        ),
                        windows,
                    )
                )
        merged: dict = {}
        merged_canon: dict = {}
        for window, (raw, _resp) in zip(windows, results, strict=True):
            # Later windows win a path collision, deterministically, because the
            # window order is the plan's order and the plan is a pure function
            # of the pages.
            merged.update(_facts_as_model_fields(raw, window, line_anchored=args.line_anchored))
            merged_canon.update(
                _facts_as_model_fields(raw, window, line_anchored=args.line_anchored, canonical_keys=True)
            )
        runs.append(merged)
        runs_canon.append(merged_canon)
        total_cost += float(ledger.total_usd)
        print(f"  run {n + 1}: {len(merged)} distinct paths ({time.monotonic() - started:.0f}s)")

    # Every run's paths and values, so a DIFFERENT cut of the same measurement
    # costs nothing. Three near-identical 100-window runs cost $6 and gave
    # 33.3%, 34.3% and 27.3% - a spread wider than any effect being tested -
    # which is what iterating against an unsaved measurement looks like.
    # TENANT DATA: values are in here. Write it outside the repo.
    if args.dump:
        Path(args.dump).write_text(
            json.dumps(
                [{path: _dump_value(fact) for path, fact in run.items()} for run in runs],
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"  wrote {len(runs)} runs to {args.dump}")

    # BOTH measurements over the SAME runs. Comparing two independent 3-run
    # samples cannot answer this: paths per run swing ~10% between samples, so
    # a real merge is indistinguishable from a quieter draw. The only honest
    # comparison applies the transformation offline to identical data.
    if args.canonical_keys:
        raw_report = measure_determinism(runs)
        canon_report = measure_determinism(runs_canon)
        print("\nSAME RUNS, keys raw vs canonicalised:")
        print(f"  {'':<20} {'raw':>8} {'canonical':>10}")
        print(f"  {'distinct paths':<20} {raw_report.total_paths:>8} {canon_report.total_paths:>10}")
        for name, attr in (
            ("stable", "stable"),
            ("flapping", "flapping"),
            ("value divergent", "value_divergent"),
            ("anchor divergent", "anchor_divergent"),
        ):
            a, b = len(getattr(raw_report, attr)), len(getattr(canon_report, attr))
            print(f"  {name:<20} {a:>8} {b:>10}")
        merged_away = raw_report.total_paths - canon_report.total_paths
        print(f"\n  paths merged by canonicalisation: {merged_away}")
        print(
            f"  stability {stability_from(raw_report).share:.1%} -> {stability_from(canon_report).share:.1%}"
        )

    # WHAT THE KEY DRIFT ACTUALLY IS.
    #
    # Canonicalisation merged nothing, which says the drift is not spelling:
    # the prompt already asks for a slug, so the model returns `riddell_street`
    # rather than "Riddell Street Pty. Ltd.", and legal-suffix normalisation has
    # nothing to strip. What varies is how much of the label the model CHOSE to
    # keep -- `riddell_street` against `prive_21_riddell_street`.
    #
    # That is a clustering problem, not a normalisation one. This measures
    # whether clustering would even work: within one collection, how many keys
    # share most of their word stems with another key? Counts only; a key is an
    # extracted party or property name and never printed.
    import re as _re

    keys_by_collection: dict[str, set[str]] = {}
    for run in runs:
        for path in run:
            for m in _re.finditer(r"([a-z_.]+)\[([^\]]+)\]", path):
                keys_by_collection.setdefault(m.group(1), set()).add(m.group(2))

    def stems(key: str) -> set[str]:
        return {w for w in key.split("_") if len(w) > 2}

    pairs = overlapping = 0
    collections_with_overlap = 0
    for _collection, keys in keys_by_collection.items():
        ordered = sorted(keys)
        found = False
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                pairs += 1
                sa, sb = stems(a), stems(b)
                if sa and sb and (sa <= sb or sb <= sa):
                    overlapping += 1
                    found = True
        if found:
            collections_with_overlap += 1

    total_keys = sum(len(v) for v in keys_by_collection.values())
    print(f"\nkey drift shape ({len(keys_by_collection)} keyed collections, {total_keys} distinct keys)")
    print(f"  distinct key PAIRS within a collection          {pairs:>6}")
    print(f"  ... where one key's stems CONTAIN the other's   {overlapping:>6}")
    if pairs:
        print(f"  ... share                                       {overlapping / pairs:>6.1%}")
    print(f"  collections holding at least one such pair      {collections_with_overlap:>6}")
    print("  (a contained pair is one label truncated, which clustering would merge)")

    report = measure_determinism(runs_canon if args.canonical_keys else runs)
    stability = stability_from(report)

    print(f"\n{report.total_paths} distinct paths across {report.n_runs} runs  (${total_cost:.2f})")
    if report.note:
        print(f"  {report.note}")
        return 1

    def share(count: int) -> str:
        return f"{count / report.total_paths:>6.1%}" if report.total_paths else "     -"

    print(f"  stable            {len(report.stable):>5}  {share(len(report.stable))}")
    print(f"  flapping          {len(report.flapping):>5}  {share(len(report.flapping))}")
    print(f"  value divergent   {len(report.value_divergent):>5}  {share(len(report.value_divergent))}")
    print(f"  anchor divergent  {len(report.anchor_divergent):>5}  {share(len(report.anchor_divergent))}")

    print(f"\nstability {stability.share:.1%} against a {STABILITY_BAR:.0%} bar — ", end="")
    print("PASS" if stability.passes else "FAIL")

    # KEYED vs PLAIN. A keyed path carries an extracted value inside it —
    # `valuation.comp_set[<slug>]` — and `slugify` only makes the SPELLING of a
    # label deterministic, not the choice of label. Two runs naming the same
    # comparable differently produce two paths, which reads as one flapping
    # path in each direction rather than as one entity described twice.
    #
    # If instability concentrates in keyed paths, entity canonicalisation is
    # the fix. If it is spread evenly, the problem is deeper than keys and a
    # registry would not touch it.
    def keyed(path: str) -> bool:
        return "[" in path

    buckets = {
        "stable": list(report.stable),
        "flapping": [f.path for f in report.flapping],
        "value divergent": [d.path for d in report.value_divergent],
        "anchor divergent": [a.path for a in report.anchor_divergent],
    }
    print(f"\n{'bucket':<18} {'keyed':>7} {'plain':>7}   keyed share")
    for name, paths in buckets.items():
        k = sum(1 for p in paths if keyed(p))
        plain = len(paths) - k
        pct = f"{k / len(paths):.0%}" if paths else "-"
        print(f"  {name:<16} {k:>7} {plain:>7}   {pct:>6}")

    # BY DECLARED KIND — the cut that decides what "stable enough" means.
    #
    # D11 splits validation into Track 1 (numbers: 100% reconciled-or-explained)
    # and Track 2 (prose: rubric, blind review). A credit paper's NUMBERS are
    # the thing a stability bar is protecting; a paraphrased `relying_party` or
    # `market.conditions` summary is prose, and two runs wording it differently
    # is not the same defect as two runs disagreeing about a facility limit.
    #
    # `STABILITY_BAR` is one number over every path, so it reports those two as
    # one thing. This says which.
    from creditpaper.ingest.harvest_prompt import field_kinds, normalise_key_path

    kinds = field_kinds(FIELD_REFERENCE.read_text(encoding="utf-8"))

    def kind_of(path: str) -> str:
        return kinds.get(normalise_key_path(path), "unknown")

    by_kind: dict[str, dict[str, int]] = {}
    for name, paths in buckets.items():
        for path in paths:
            row = by_kind.setdefault(kind_of(path), {})
            row[name] = row.get(name, 0) + 1
    print(f"\n{'declared kind':<14} {'paths':>6} {'stable':>7} {'share':>7}   (bar 98%)")
    for kind_name in sorted(by_kind, key=lambda k: -sum(by_kind[k].values())):
        row = by_kind[kind_name]
        total_k = sum(row.values())
        stable_k = row.get("stable", 0)
        share = stable_k / total_k if total_k else 0.0
        mark = "PASS" if share >= 0.98 else ""
        print(f"  {kind_name:<12} {total_k:>6} {stable_k:>7} {share:>6.1%}   {mark}")

    # PRESENCE DISTRIBUTION — the multi-pass question.
    #
    # LangExtract defaults to `extraction_passes=3` and frames flapping as a
    # RECALL problem: passes are unioned, so a fact found in one pass of three
    # is found, not discarded. That inverts how flapping reads here. A path at
    # 3/3 is a fact every pass agrees exists; 1/3 is a fact one pass alone saw.
    #
    # The distribution is what a consensus design is built on: it says how many
    # passes buy how much, and what a pass count is worth as a confidence
    # signal.
    presence: dict[int, int] = {}
    for path in set().union(*(set(r) for r in runs)) if runs else set():
        seen_in = sum(1 for r in runs if path in r and r[path].value is not None)
        presence[seen_in] = presence.get(seen_in, 0) + 1
    union = sum(presence.values())
    print(f"\npresence across {report.n_runs} passes (union = {union} paths):")
    for count in sorted(presence, reverse=True):
        n = presence[count]
        print(f"  seen in {count}/{report.n_runs}   {n:>5}  {n / union:>6.1%}")
    mean_run = sum(len(r) for r in runs) / len(runs)
    print(f"  a single pass finds {mean_run:.0f} on average; the union is {union}")
    print(f"  so a second+ pass adds {union - mean_run:.0f} paths ({union / mean_run - 1:.0%} more)")

    all_keyed = sum(1 for paths in buckets.values() for p in paths if keyed(p))
    unstable_keyed = sum(1 for name, paths in buckets.items() if name != "stable" for p in paths if keyed(p))
    if all_keyed:
        print(
            f"\n  {unstable_keyed} of {all_keyed} keyed paths are unstable ({unstable_keyed / all_keyed:.0%})"
        )

    if report.value_divergent:
        print("\nvalue divergent paths (the dangerous bucket — settled-looking, different answers):")
        for d in report.value_divergent[:15]:
            print(f"  {redact(d.path)}")
    if report.anchor_divergent:
        print("\nanchor divergent paths (value holds, citation moves):")
        for a in report.anchor_divergent[:10]:
            print(f"  {redact(a.path)}")
    if report.flapping:
        print(f"\nflapping paths ({len(report.flapping)} total, first 15):")
        for f in report.flapping[:15]:
            print(f"  {f.present_count}/{f.total_runs}  {redact(f.path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
