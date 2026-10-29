"""Corpus-path scorecard: the SHIPPED deal-wide ingestion, scored against ground truth.

`scripts/ingest_scorecard.py` scores the per-slot extractors. The deal-wide path
production runs (`deals/utils/ingest.py::ingest_corpus`: harvest, material pass,
consensus, verification, apply, derive) had no ground-truth score, so changes to
it - sampling temperature, the number of passes, the material pass - could be
measured for stability (`general_extraction_determinism.py`) but not for whether
they find the RIGHT facts. This runs `ingest_corpus` itself on a deal folder and
classifies every ground-truth field:

  correct_verified  right value, passed the gate (no analyst needed)
  false_flag        right value, held for review
  catch             wrong value, held for review (the system caught it)
  FALSE_PASS        wrong value, passed the gate (the failure that matters)
  missing           no value
  derived           computed from other facts (formula, not a quote)

"Passed the gate" is production's: status extracted/confirmed and confidence at
or above `EXTRACTION_CONFIDENCE_THRESHOLD`.

SPENDS. Every arm's cost is projected from the window count before it starts
and refused when the run would pass --budget-usd; the ledger's actual spend is
what is reported. Page text is parsed once per file (production parser and OCR,
optionally vision) and cached in --cache, so a second arm parses nothing.

TENANT DATA: prints paths and counts only. --out receives per-field rows WITH
values; keep it outside the repo. Uses DATABASE_URL (the vision parse cache), so
point it at a local database.

Usage:
    python scripts/corpus_scorecard.py --deal blind_03 --arm t0:1 --arm default:3 --budget-usd 40 \\
        --out <dir outside the repo> [--vision]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Measured 2026-10-08 on blind_03 at production's 2-page window: a Haiku 4.5
# harvest call costs ~$0.003 and a Sonnet 5 one ~$0.015; the material pass is
# priced like a harvest call until it has been measured. Used only to
# REFUSE a run that would pass the budget; the ledger reports what was spent.
HARVEST_USD_PER_WINDOW = 0.010  # Sonnet 5 measured ~$0.030/window = 3x this; Haiku ~$0.0027
MATERIAL_USD_PER_WINDOW = 0.012  # Haiku measured ~$0.009/window uncached (blind_01)
# Arm models, priced relative to Haiku 4.5 ($1/$5 per M tokens) for the projection.
# (registry choice, measured $ per 2-page harvest window): Haiku 0.0027 and Sonnet 0.030 on
# blind_01/03 slotted documents, 2026-10-08. The projection adds 50% for list passes.
MODELS = {
    "haiku": ("anthropic:claude-haiku-4-5-20251001", 0.0027 / HARVEST_USD_PER_WINDOW),
    "sonnet5": ("anthropic:claude-sonnet-5", 0.030 / HARVEST_USD_PER_WINDOW),
}
VISION_USD_PER_PAGE_EST = 0.018  # measured 2026-10-08: 40 blind_03 pages, Haiku 4.5, 150 dpi
TENANT = "00000000-0000-0000-0000-000000005c0d"


def _deal_dir(deal: str, fixtures: Path) -> Path:
    for cand in (fixtures / deal, fixtures / "blind" / deal, Path(deal)):
        if (cand / "ground_truth.json").exists():
            return cand
    raise SystemExit(f"no deal folder with ground_truth.json for {deal!r}")


def _parse(files: list[Path], cache: Path, *, vision: bool) -> dict[Path, list[dict[str, Any]]]:
    from creditpaper.ingest.page_text import PARSER_VERSION
    from creditpaper.processing.extractors.pdf import extract_pdf

    cache.mkdir(parents=True, exist_ok=True)
    out: dict[Path, list[dict[str, Any]]] = {}
    for path in files:
        raw = path.read_bytes()
        key = hashlib.sha256(raw + PARSER_VERSION.encode() + (b"+vision" if vision else b"")).hexdigest()
        cached = cache / f"{key}.json"
        if cached.exists():
            out[path] = json.loads(cached.read_text(encoding="utf-8"))
            continue
        result = extract_pdf(raw, original_filename="document.pdf")
        if vision:
            from creditpaper.processing.vision import enrich

            result = enrich(TENANT, raw, result)
        cached.write_text(json.dumps(result.page_texts), encoding="utf-8")
        out[path] = result.page_texts
    return out


def _vision_cached(path: Path, cache: Path) -> bool:
    from creditpaper.ingest.page_text import PARSER_VERSION

    key = hashlib.sha256(path.read_bytes() + PARSER_VERSION.encode() + b"+vision").hexdigest()
    return (cache / f"{key}.json").exists()


def _machine_read_count(pages: dict[Path, list[dict[str, Any]]]) -> int:
    return sum(1 for entries in pages.values() for p in entries if p.get("ocr_used"))


def _score(model: Any, gt: Any) -> list[dict[str, Any]]:
    from creditpaper.config import EXTRACTION_CONFIDENCE_THRESHOLD
    from creditpaper.harness.reconcile import Verdict, reconcile
    from creditpaper.model.citations import resolve_path
    from creditpaper.model.concepts import concept_for
    from creditpaper.model.core import ExtractionMethod, Fact, FactStatus

    rows: list[dict[str, Any]] = []
    for line in reconcile(model, gt).lines:
        if line.verdict is Verdict.UNMODELLED:
            continue
        correct = line.verdict in (Verdict.MATCH, Verdict.EXPLAINED)
        concept = concept_for(line.path)
        row = {
            "path": line.path,
            "material": bool(concept and concept.material),
            "expected": line.expected,
            "extracted": line.actual,
        }
        fact = resolve_path(model, line.path)
        if line.actual is None or not isinstance(fact, Fact):
            row["category"] = "missing_explained" if correct else "missing"
        elif fact.provenance is not None and fact.provenance.method is ExtractionMethod.DERIVED:
            row["category"] = "derived"
        else:
            passed = (
                fact.status in (FactStatus.EXTRACTED, FactStatus.CONFIRMED)
                and (fact.confidence or 0.0) >= EXTRACTION_CONFIDENCE_THRESHOLD
            )
            if correct:
                row["category"] = "correct_verified" if passed else "false_flag"
            else:
                row["category"] = "FALSE_PASS" if passed else "catch"
        rows.append(row)
    return rows


def _report(name: str, rows: list[dict[str, Any]], cost: float, seconds: float) -> dict[str, Any]:
    counts = Counter(r["category"] for r in rows)
    material = Counter(r["category"] for r in rows if r["material"])
    print(f"\n{name}: {len(rows)} ground-truth fields  (${cost:.2f}, {seconds:.0f}s)")
    for cat in (
        "correct_verified",
        "false_flag",
        "catch",
        "FALSE_PASS",
        "missing",
        "missing_explained",
        "held",
        "text_differs",
        "not_from_documents",
        "not_in_documents",
        "derived",
    ):
        print(f"  {cat:<18} {counts.get(cat, 0):>4}    material {material.get(cat, 0):>3}")
    return {
        "arm": name,
        "counts": dict(counts),
        "material": dict(material),
        "cost_usd": cost,
        "seconds": seconds,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--deal", required=True, help="blind_01, blind_02, blind_03, build_deal or a folder")
    ap.add_argument("--fixtures", type=Path, default=ROOT.parent / "creditpaper" / "fixtures")
    ap.add_argument(
        "--arm",
        action="append",
        required=True,
        help="[model@]temperature:passes, e.g. haiku@t0:1 or sonnet5@default:1 (no model: the registry's)",
    )
    ap.add_argument("--budget-usd", type=float, required=True)
    ap.add_argument("--vision", action="store_true", help="vision on machine-read pages (spends)")
    ap.add_argument("--out", type=Path, required=True, help="per-field rows WITH values: outside the repo")
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--material", action="store_true", help="run the per-window material pass (spends)")
    ap.add_argument("--window", type=int, default=0, help="pages per window (0: production's)")
    ap.add_argument(
        "--rescore-deal",
        default=None,
        help="re-score a ledger deal an earlier run committed (no parsing, no model calls, no spend)",
    )
    ap.add_argument("--no-lists", action="store_true", help="skip the targeted list passes")
    ap.add_argument(
        "--slotted-only",
        action="store_true",
        help="read only documents the matcher slots, as production reads only analyst-placed documents (D3)",
    )
    args = ap.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(ROOT.parent / "creditpaper" / ".env")
    load_dotenv(ROOT / ".env")

    from creditpaper.deals.utils.ingest import ingest_corpus
    from creditpaper.harness.reconcile import GroundTruth
    from creditpaper.ingest.corpus import plan_corpus
    from creditpaper.ingest.general import GENERAL_WINDOW, pages_from_map
    from creditpaper.model import DealModel

    ddir = _deal_dir(args.deal, args.fixtures)
    gt = GroundTruth.load(ddir / "ground_truth.json")
    if args.rescore_deal:
        import _scorecard_production as prod

        from creditpaper.ledger import contradictions, service

        loaded = service.load_model(TENANT, args.rescore_deal)
        held = {
            r.concept_path
            for r in contradictions.list_contradictions(TENANT, args.rescore_deal)
            if r.blocking
        }
        files = sorted(
            p for p in (ddir / "deal_files").rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"
        )
        texts = _parse(files, args.cache or (args.out / "parse_cache"), vision=False) if args.cache else {}
        doc_figures = (
            prod.document_figures(p.get("text") or "" for entries in texts.values() for p in entries)
            if texts
            else None
        )
        rows = prod.score(loaded, gt, held, doc_figures)
        paper = ddir / "_extracted_paper.txt"
        recall = (
            prod.paper_recall(paper.read_text(encoding="utf-8", errors="ignore"), loaded)
            if paper.exists()
            else {}
        )
        _report(f"rescore {args.rescore_deal[:8]}", rows, 0.0, 0.0)
        if recall:
            print(f"  paper figures found {recall['found']} of {recall['paper_figures']}")
        return 0
    files = sorted(p for p in (ddir / "deal_files").rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    cache = args.cache or (args.out / "parse_cache")
    args.out.mkdir(parents=True, exist_ok=True)

    spent = 0.0
    vision_usd = 0.0
    t0 = time.monotonic()
    plain = _parse(files, cache, vision=False)
    parse_s = time.monotonic() - t0  # zero-ish when the parse cache already held these files
    vision_s = 0.0
    # Production parses with Tesseract only; vision is targeted after extraction
    # (ingest/vision_check.py) and its cost is in each arm's ledger.
    pages = plain

    import _scorecard_production as prod

    # Production reads only documents with a confirmed slot or none, and never a refused class (D7).
    deal_type = (
        json.loads((ddir / "ground_truth.json").read_text(encoding="utf-8")).get("deal_type")
        or "construction"
    )
    slot_of, refused = prod.assign_slots(files, pages, deal_type)
    read_files = [f for f in files if f not in refused and (slot_of.get(f) or not args.slotted_only)]
    pages = {f: pages[f] for f in read_files}
    window = args.window or GENERAL_WINDOW
    document_ids = {path: (f"doc-{i}", f"ver-{i}") for i, path in enumerate(read_files, start=1)}
    windows = plan_corpus(pages_from_map(pages, document_ids), window=window).calls
    slotted = sum(1 for f in read_files if slot_of.get(f))
    print(f"{len(refused)} documents refused (D7); {slotted} of {len(read_files)} read documents slotted")
    paper = ddir / "_extracted_paper.txt"
    paper_text = paper.read_text(encoding="utf-8", errors="ignore") if paper.exists() else ""
    total_pages = sum(len(entries) for entries in pages.values())
    machine = _machine_read_count(pages)
    agreed = sum(1 for entries in pages.values() for p in entries if p.get("ocr_agrees"))
    print(
        f"{len(files)} documents, {total_pages} pages, {windows} windows, {machine} machine-read pages "
        f"({agreed} confirmed by vision); parse {parse_s:.0f}s"
    )

    results = []
    import os

    for arm in args.arm:
        model_key, _, sampling = arm.rpartition("@")
        label, _, passes_text = sampling.partition(":")
        passes = int(passes_text or "1")
        temperature = None if label == "default" else float(label.lstrip("t") or 0)
        model_id, price = MODELS[model_key] if model_key else (None, 3.0)
        if model_id:
            os.environ["LLM_TASK_EXTRACT"] = model_id  # the registry override ingest_corpus resolves
        else:
            os.environ.pop("LLM_TASK_EXTRACT", None)
        material_usd = MATERIAL_USD_PER_WINDOW if args.material else 0.0
        lists_factor = 1.0 if args.no_lists else 1.5
        projected = price * windows * (passes * HARVEST_USD_PER_WINDOW * lists_factor + material_usd)
        if spent + projected > args.budget_usd:
            print(f"\n{arm}: projected ${projected:.2f} would pass the budget (${spent:.2f} spent); not run")
            continue
        ledger_deal, document_ids = prod.seed_ledger(TENANT, {f: slot_of.get(f) for f in read_files})
        model = DealModel(tenant_id=TENANT, deal_id=ledger_deal)
        started = time.monotonic()
        rep = ingest_corpus(
            model,
            pages,
            document_ids=document_ids,
            tenant_id=TENANT,
            deal_id=ledger_deal,
            passes=passes,
            window=window,
            use_cache=False,
            support=False,
            material=args.material,
            temperature=temperature,
            # Each document's slot, as production knows it once the analyst has placed it (D3).
            slot_of={document_ids[f][0]: slot_of[f] for f in read_files if slot_of.get(f)},
            lists=not args.no_lists,
            vision=args.vision,
        )
        spent += rep.cost_usd
        (args.out / f"{args.deal}_{arm.replace(':', '_').replace('@', '_')}_outputs.json").write_text(
            json.dumps(
                [{k: f.model_dump(mode="json") for k, f in o.fields.items()} for o in rep.outputs],
                default=str,
            ),
            encoding="utf-8",
        )
        loaded, held = prod.commit_and_load(TENANT, ledger_deal, rep.outputs)
        # Every document of the deal, read or not: a figure an unslotted document states is genuinely missing.
        doc_figures = prod.document_figures(
            p.get("text") or "" for entries in plain.values() for p in entries
        )
        rows = prod.score(loaded, gt, held, doc_figures)
        recall = prod.paper_recall(paper_text, loaded) if paper_text else {}
        summary = _report(arm, rows, rep.cost_usd, time.monotonic() - started)
        summary["material_not_stated"] = len(rep.material_not_stated)
        summary["paper_recall"] = recall
        summary["ledger_deal"] = ledger_deal
        if recall:
            share = recall["found"] / recall["paper_figures"] if recall["paper_figures"] else 0.0
            print(f"  paper figures found {recall['found']} of {recall['paper_figures']} ({share:.0%})")
        # Where the money went: per-tool tokens (no document content).
        print(rep.cost_summary)
        summary["cost_summary"] = rep.cost_summary
        summary.update(
            documents=len(files),
            pages=total_pages,
            windows=windows,
            machine_read_pages=machine,
            vision_confirmed_pages=agreed,
            parse_s=parse_s,
            vision_s=vision_s,
            vision_usd=vision_usd,
        )
        results.append(summary)
        (args.out / f"{args.deal}_{arm.replace(':', '_')}.json").write_text(
            json.dumps({"summary": summary, "rows": rows}, indent=2, default=str), encoding="utf-8"
        )
    print(f"\ntotal spent ${spent:.2f} of ${args.budget_usd:.2f}")
    with (args.out / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"deal": args.deal, "arms": results, "spent": spent}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
