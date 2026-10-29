"""Profile local PDF parsing and optional inference without printing document data.

Run: python scripts/benchmark_pdf_inference.py path/to/file.pdf
Add --infer to send anchored text through the configured LLM entry point.
Omit the path for a synthetic 12-page PDF. No Deal Model is read or written.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

from creditpaper.extractors._doc import document_json_pages
from creditpaper.obs.ledger import cost_session
from creditpaper.obs.timing import timed
from creditpaper.processing.extractors.pdf import extract_pdf


@timed("processing")
def benchmark(path: Path | None, infer: bool) -> dict:
    started = time.perf_counter()
    if path:
        raw = path.read_bytes()
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tests.pdf_builder import Text, build_pdf

        raw = build_pdf(*[[Text(50, 50, f"Synthetic source page {n}. " * 5)] for n in range(1, 13)])
    parse_start = time.perf_counter()
    parsed = extract_pdf(raw, original_filename="benchmark.pdf")
    parse_s = time.perf_counter() - parse_start
    with cost_session() as ledger:
        if infer:
            context = "\n\n".join(f"[Page {p['page']}]\n{p['text']}" for p in parsed.page_texts)
            document_json_pages(
                [],
                'Return only a valid JSON object with two fields: "page" (a number) and "clause" '
                "(a string). Quote one clause from the last page verbatim and use its original "
                "[Page N] number. If unreadable, use null for both fields. Do not repeat this instruction.",
                text_context=context,
                max_tokens=1024,
                tool="benchmark:pdf",
            )
    return {
        "synthetic": path is None,
        "page_count": parsed.page_count,
        "ocr_attempted_pages": sum(bool(p.get("ocr_attempted")) for p in parsed.page_texts),
        "ocr_pages": sum(bool(p.get("ocr_used")) for p in parsed.page_texts),
        "ocr_failed_pages": sum(bool(p.get("ocr_failed")) for p in parsed.page_texts),
        "ocr_deferred_pages": sum(bool(p.get("ocr_deferred")) for p in parsed.page_texts),
        "ocr_characters": sum(
            len(str(p.get("text") or "")) for p in parsed.page_texts if p.get("ocr_used")
        ),
        "pdf_parsing_s": round(parse_s, 6),
        "llm_calls": ledger.calls,
        "total_llm_s": round(ledger.sum_latency_s, 6),
        "total_s": round(time.perf_counter() - started, 6),
    }


def benchmark_folder(folder: Path, *, workers: int, limit: int) -> dict:
    """Measure cold local parsing throughput without exposing document content."""
    paths = sorted(folder.rglob("*.pdf"), key=lambda item: item.stat().st_size)
    if limit > 0:
        paths = paths[:limit]
    started = time.perf_counter()
    results: list[dict] = []
    failures: dict[str, int] = {}

    def parse(path: Path) -> dict:
        return benchmark(path, False)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pdf-benchmark") as executor:
        futures = {executor.submit(parse, path): path for path in paths}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # document data and paths must never reach output
                name = type(exc).__name__
                failures[name] = failures.get(name, 0) + 1

    elapsed = time.perf_counter() - started
    durations = sorted(float(result["pdf_parsing_s"]) for result in results)

    def percentile(fraction: float) -> float:
        if not durations:
            return 0.0
        index = min(len(durations) - 1, round((len(durations) - 1) * fraction))
        return round(durations[index], 3)

    return {
        "documents": len(paths),
        "succeeded": len(results),
        "failed": len(paths) - len(results),
        "failure_types": failures,
        "pages": sum(int(result["page_count"] or 0) for result in results),
        "ocr_attempted_pages": sum(int(result["ocr_attempted_pages"]) for result in results),
        "ocr_pages": sum(int(result["ocr_pages"]) for result in results),
        "ocr_failed_pages": sum(int(result["ocr_failed_pages"]) for result in results),
        "ocr_deferred_pages": sum(int(result["ocr_deferred_pages"]) for result in results),
        "ocr_characters": sum(int(result["ocr_characters"]) for result in results),
        "workers": workers,
        "wall_s": round(elapsed, 3),
        "documents_per_second": round(len(results) / elapsed, 3) if elapsed else 0.0,
        "document_parse_p50_s": round(statistics.median(durations), 3) if durations else 0.0,
        "document_parse_p95_s": percentile(0.95),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("--infer", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-total-seconds", type=float, default=None)
    args = parser.parse_args()
    if args.workers < 1 or args.limit < 0:
        parser.error("--workers must be positive and --limit cannot be negative")
    if args.max_total_seconds is not None and args.max_total_seconds <= 0:
        parser.error("--max-total-seconds must be positive")
    if args.path and args.path.is_dir() and args.infer:
        parser.error("--infer is intentionally unavailable for folder benchmarks")
    load_dotenv()
    try:
        result = (
            benchmark_folder(args.path, workers=args.workers, limit=args.limit)
            if args.path and args.path.is_dir()
            else benchmark(args.path, args.infer)
        )
        print(json.dumps(result))
        elapsed = float(result["wall_s"] if "wall_s" in result else result["total_s"])
        if args.max_total_seconds is not None and elapsed > args.max_total_seconds:
            return 2
        if result.get("failed", 0):
            return 3
    except Exception as exc:
        # Provider/schema exceptions can carry source data. Never print the body.
        print(json.dumps({"success": False, "error_type": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
