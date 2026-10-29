"""Real-fixture ingestion scorecard: the one loop every ingestion change is judged by.

    uv run python scripts/ingest_scorecard.py --deal build_deal [--cache DIR] [--no-llm]
    uv run python scripts/ingest_scorecard.py --labels tenant=<uuid>[,deal=<id>][,extractor=<v>]
    uv run python scripts/ingest_scorecard.py --deal <folder> --labels tenant=<uuid>,deal=<id>

Answer key. By default each deal folder's ground_truth.json. With --labels the answer
key is the tenant's label store (spec 3c: analyst decisions are the ground truth):
- with --deal, the folder is extracted as usual and scored against the latest label
  per concept for the labelled deal (seed and analyst labels alike);
- without --deal, the recorded decisions themselves are scored ("accuracy from use"):
  per concept, the extracted value against the analyst's final value, under the
  verdict the system gave at decision time. FALSE PASS = the system passed the fact
  (verified, not flagged) and the analyst's final value differs.

1. Extraction: route the deal's PDFs to slots (harness.route), run each slot's
   extractor with the pipeline's page texts, cache every ExtractionOutput on disk
   keyed by file hash + extractor version (source hash of the extractor, the shared
   extraction plumbing, the page selector, the extract model and the provider).
   Re-runs cost nothing. LLM calls go through the shared client; cost comes from the
   cost ledger. --no-llm uses the cache only (a miss is reported, not extracted).
2. Page text: pypdfium2 if installed, else the pipeline's own extract_pdf (PyMuPDF
   with OCR fallback). Cached by file hash + reader version (reader.READER_VERSION).
   --vision: text-less pages (< 20 non-whitespace characters) are transcribed once
   by the vision model (ingest.transcribe) into the parse cache (DATABASE_URL), and
   the transcription is the page text for both extraction and verification. The
   spend is estimated first and refused over --vision-budget-usd. A fact verified
   on a vision page carries VISION_TEXT and a confidence below the review threshold.
3. Verification and commit: the production path (ingest.commit). Each document's
   facts go through verify_and_build (kind and materiality from the concept, the deal
   currency, the field path, vision pages) and are committed by concept through
   model/apply.py; an unmapped path is refused. Scoring reads the verification stored
   on each committed fact. --support turns on the semantic support leg for material
   facts (spends: one model call per material fact); it is off by default.
4. Comparison: harness.reconcile against ground_truth.json; each ground-truth field
   is classified correct+verified / false flag / catch / FALSE PASS / missing /
   unanchored (derived facts are counted apart: they carry a formula, not a quote).
5. Output: a compact summary on stdout (paths and counts only, never document text)
   and per-fact CSV + JSON in --out, plus a history.jsonl line per run.

Fixtures are tenant data: this script reads them, it never writes into fixtures/ and
nothing it writes belongs in git.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import inspect
import json
import math
import os
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from creditpaper.concurrency import pool  # noqa: E402
from creditpaper.harness.reconcile import GroundTruth, Verdict, reconcile  # noqa: E402
from creditpaper.harness.route import EXTRACTOR_REGISTRY, route_deal  # noqa: E402
from creditpaper.ingest.commit import (  # noqa: E402
    CheckerFactory,
    commit,
    default_checker_factory,
    verify_and_build,
)
from creditpaper.ingest.verify import FactVerification  # noqa: E402
from creditpaper.model import (  # noqa: E402
    DealModel,
    ExtractionOutput,
    derive_delivery_model,
    derive_economics,
    derive_metrics,
)
from creditpaper.model.citations import resolve_path  # noqa: E402
from creditpaper.model.core import ExtractionMethod, Fact  # noqa: E402
from creditpaper.obs.ledger import cost_session  # noqa: E402

SCORECARD_VERSION = "1"
# The parse cache is tenant-scoped (a uuid): the scorecard's own tenant.
SCORECARD_TENANT = "00000000-0000-0000-0000-000000005c0d"
# Estimated cost of one page transcription (one ~1568px page image in, up to a
# page of text out, at the extract model's price). Used only to refuse a run
# before it spends, never to report spend (the cost ledger does that).
VISION_USD_PER_PAGE_EST = 0.03
_VERIFIED = {"verified", "anchored"}
_PAGE_ONLY = re.compile(r"^\s*(page\s*\d+|covenant clause)?\s*$", re.IGNORECASE)

CATEGORIES = (
    "correct_verified",
    "false_flag",  # correct but flagged
    "catch",  # wrong and flagged
    "FALSE_PASS",  # wrong but verified: the key number
    "missing",
    "unanchored",
    "derived",
)


# -- paths ---------------------------------------------------------------------


def default_fixtures() -> Path:
    env = os.getenv("CREDITPAPER_FIXTURES")
    if env:
        return Path(env)
    for cand in (ROOT / "fixtures", ROOT.parent / "creditpaper" / "fixtures"):
        if (cand / "build_deal").exists():
            return cand
    return ROOT / "fixtures"


def deal_dir(fixtures: Path, deal: str, *, need_ground_truth: bool = True) -> Path:
    for cand in (fixtures / deal, fixtures / "blind" / deal, Path(deal)):
        if (cand / "ground_truth.json").exists() or (not need_ground_truth and cand.is_dir()):
            return cand
    raise SystemExit(f"deal not found: {deal}")


# -- hashing / caching -----------------------------------------------------------


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _source_sha(objs: list[Any]) -> str:
    h = hashlib.sha256()
    for obj in objs:
        try:
            src = inspect.getsourcefile(obj)
            if src:
                h.update(Path(src).read_bytes())
        except TypeError:
            h.update(repr(obj).encode())
    return h.hexdigest()[:12]


def extractor_version(cls: type) -> str:
    """Changes whenever the extractor, its shared plumbing, the model or provider does."""
    from creditpaper import __version__
    from creditpaper.config import extract_model
    from creditpaper.extractors import _doc, base
    from creditpaper.processing import page_selector

    provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower() or "anthropic"
    code = _source_sha([cls, _doc, base, page_selector])
    return f"{__version__}-{code}-{extract_model()}-{provider}".replace("/", "_")


def _enc(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"t": "bool", "v": v}
    if isinstance(v, Decimal):
        return {"t": "dec", "v": str(v)}
    if isinstance(v, int):
        return {"t": "int", "v": v}
    if isinstance(v, float):
        return {"t": "float", "v": v}
    if isinstance(v, datetime):
        return {"t": "datetime", "v": v.isoformat()}
    if isinstance(v, date):
        return {"t": "date", "v": v.isoformat()}
    if v is None or isinstance(v, str):
        return {"t": "raw", "v": v}
    return {"t": "raw", "v": json.loads(json.dumps(v, default=str))}


def _dec(d: dict[str, Any]) -> Any:
    t, v = d["t"], d.get("v")
    if t == "dec":
        return Decimal(v)
    if t == "datetime":
        return datetime.fromisoformat(v)
    if t == "date":
        return date.fromisoformat(v)
    return v


def output_to_json(out: ExtractionOutput) -> dict[str, Any]:
    fields = {}
    for path, fact in out.fields.items():
        dumped = fact.model_dump(mode="json", exclude={"value"})
        dumped["value"] = _enc(fact.value)
        fields[path] = dumped
    return {"fields": fields, "flags": list(out.flags)}


def output_from_json(data: dict[str, Any]) -> ExtractionOutput:
    fields = {}
    for path, dumped in data["fields"].items():
        body = dict(dumped)
        body["value"] = _dec(body["value"])
        fields[path] = Fact.model_validate(body)
    return ExtractionOutput(fields=fields, flags=list(data["flags"]))


# -- page text -------------------------------------------------------------------


def _pdfium_available() -> bool:
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        return False
    return True


def _read_pages(path: Path) -> tuple[str, list[dict[str, Any]]]:
    if _pdfium_available():
        # Only creditpaper.ingest.reader imports pypdfium2 directly (see its
        # module docstring): PdfiumReader also serialises every pdfium call
        # behind a lock, which matters here because run_extraction's own
        # page-text stage reads several documents concurrently through a
        # thread pool, and libpdfium is not thread-safe under that (silently
        # empty pages, then process-wide corruption -- see reader.py).
        from creditpaper.ingest.reader import PdfiumReader

        with PdfiumReader(path) as reader:
            pages = [
                {"page": i, "text": reader.page(i).text, "ocr_used": False}
                for i in range(1, reader.page_count() + 1)
            ]
        return "pdfium", pages
    from creditpaper.processing.extractors.pdf import extract_pdf

    result = extract_pdf(path.read_bytes(), original_filename="document.pdf")
    return "pipeline", list(result.page_texts)


def page_text_tag() -> str:
    """The reader that produced the page text, with its version.

    Part of the page-text cache key: keyed by file hash alone, the cache served
    text read before a reader fix (the pdfium lock, annotation flattening) as if
    the fixed reader had read it.
    """
    if _pdfium_available():
        from creditpaper.ingest.reader import READER_VERSION

        return READER_VERSION
    return "pipeline"


def page_texts(path: Path, sha: str, cache: Path) -> list[dict[str, Any]]:
    f = cache / "pages" / f"{sha[:16]}-{page_text_tag()}.json"
    if f.exists():
        data: list[dict[str, Any]] = json.loads(f.read_text(encoding="utf-8"))
        return data
    _, pages = _read_pages(path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(pages), encoding="utf-8")
    return pages


def _vision_backup_file(cache: Path, d: Doc) -> Path:
    from creditpaper.ingest.transcribe import transcriber_version

    ver = hashlib.sha256(transcriber_version().encode()).hexdigest()[:12]
    return cache / "vision" / f"{d.sha[:16]}-{ver}.json"


def _vision_backup(cache: Path, d: Doc) -> dict[int, str]:
    """This document's transcriptions from the scorecard's own file backup.

    The parse cache is the store (ingest.transcribe), but the scorecard's database
    is the test database, whose fixtures truncate ingest_parse_artifacts before
    every test: a paid transcription must not be lost to a test run. The backup
    is keyed exactly as the parse-cache row is and seeds it back.
    """
    f = _vision_backup_file(cache, d)
    if not f.exists():
        return {}
    return {int(k): str(v) for k, v in json.loads(f.read_text(encoding="utf-8")).items()}


def _save_vision_backup(cache: Path, d: Doc) -> None:
    texts = {str(p["page"]): p["text"] for p in d.pages if p.get("source") == "vision"}
    if texts:
        f = _vision_backup_file(cache, d)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(texts), encoding="utf-8")


# -- extraction ------------------------------------------------------------------


@dataclass
class Doc:
    slot: str
    path: Path
    sha: str
    doc_id: str  # slot + hash: no file name ever reaches the model or the output
    pages: list[dict[str, Any]] = field(default_factory=list, repr=False)
    source: str = ""  # cache | extracted | not_cached | failed:<Exc>
    extractor_version: str = ""


@dataclass
class Extraction:
    docs: list[Doc]
    outputs: list[tuple[Doc, ExtractionOutput]]
    cost_usd: float = 0.0
    llm_calls: int = 0
    page_text_s: float = 0.0
    extract_s: float = 0.0
    vision_pages: int = 0
    vision_usd: float = 0.0


def run_extraction(
    ddir: Path,
    cache: Path,
    *,
    no_llm: bool,
    registry: Mapping[str, type] | None = None,
    workers: int = 6,
    log: Callable[[str], None] = print,
    vision: bool = False,
    vision_slots: frozenset[str] | None = None,
    vision_budget_usd: float = 0.0,
    vision_client: Any = None,
) -> Extraction:
    registry = registry or EXTRACTOR_REGISTRY
    files_dir = ddir / "deal_files" if (ddir / "deal_files").exists() else ddir
    files = sorted({str(p) for p in files_dir.rglob("*") if p.suffix.lower() == ".pdf"})
    mapping = route_deal(files)
    docs: list[Doc] = []
    for slot, val in sorted(mapping.items()):
        if slot not in registry:
            continue
        for p in val if isinstance(val, (list, tuple)) else [val]:
            path = Path(p)
            sha = file_sha(path)
            if any(d.sha == sha and d.slot == slot for d in docs):
                continue  # byte-identical copy in the same slot: extract once
            docs.append(Doc(slot=slot, path=path, sha=sha, doc_id=f"{slot}:{sha[:12]}"))

    t0 = time.perf_counter()
    with pool(min(workers, max(1, len(docs))), thread_name_prefix="scorecard-text") as ex:
        futs = {ex.submit(page_texts, d.path, d.sha, cache): d for d in docs}
        for fut in futs:
            futs[fut].pages = fut.result()
    page_text_s = time.perf_counter() - t0

    vision_docs = [d for d in docs if vision and (vision_slots is None or d.slot in vision_slots)]
    if vision_docs:
        from creditpaper.ingest import transcribe

        pending = {
            d.doc_id: transcribe.pending_pages(
                SCORECARD_TENANT, d.sha, d.pages, seed=_vision_backup(cache, d)
            )
            for d in vision_docs
        }
        n_pending = sum(len(v) for v in pending.values())
        est = n_pending * VISION_USD_PER_PAGE_EST
        log(f"vision: {n_pending} text-less page(s) to transcribe, est ${est:.2f}")
        if est > vision_budget_usd:
            raise SystemExit(f"vision: estimate ${est:.2f} is over the budget ${vision_budget_usd:.2f}")

    t1 = time.perf_counter()
    outputs: list[tuple[Doc, ExtractionOutput]] = []
    todo: list[tuple[Doc, Path]] = []
    for d in docs:
        ver = extractor_version(registry[d.slot])
        d.extractor_version = ver
        # short name: Windows MAX_PATH; the full version string is stored inside
        f = cache / "extract" / f"{d.sha[:16]}-{d.slot}-{hashlib.sha256(ver.encode()).hexdigest()[:12]}.json"
        if f.exists():
            outputs.append((d, output_from_json(json.loads(f.read_text(encoding="utf-8")))))
            d.source = "cache"
        elif no_llm:
            d.source = "not_cached"
        else:
            todo.append((d, f))

    with cost_session(deal_id=ddir.name) as ledger:
        vision_pages = 0
        if vision_docs:
            from creditpaper.extractors._doc import pages_to_png
            from creditpaper.ingest import transcribe

            for d in vision_docs:
                d.pages = transcribe.fill_text_less(
                    SCORECARD_TENANT,
                    d.sha,
                    d.pages,
                    lambda idx, path=d.path: pages_to_png(path, idx),
                    client=vision_client,
                    seed=_vision_backup(cache, d),
                )
                _save_vision_backup(cache, d)  # at once: these pages are paid for
                vision_pages += len(transcribe.vision_pages(d.pages))
        vision_usd = ledger.total_usd
        if todo:
            log(f"extracting {len(todo)} document(s) with the LLM...")
            with pool(min(workers, len(todo)), thread_name_prefix="scorecard-extract") as ex:
                efuts = {
                    ex.submit(registry[d.slot]().extract, d.doc_id, d.path, page_texts=d.pages): (d, f)
                    for d, f in todo
                }
                for fut in efuts:
                    d, f = efuts[fut]
                    try:
                        out = fut.result()
                    except Exception as exc:  # noqa: BLE001 - one doc's failure must not lose the rest
                        d.source = f"failed:{type(exc).__name__}"
                        continue
                    outputs.append((d, out))
                    d.source = "extracted"
                    try:
                        f.parent.mkdir(parents=True, exist_ok=True)
                        f.write_text(json.dumps(output_to_json(out)), encoding="utf-8")
                    except OSError as exc:  # never lose a paid-for extraction to a cache error
                        log(f"cache write failed for {d.doc_id}: {type(exc).__name__}")
    outputs.sort(key=lambda t: (t[0].slot, t[0].doc_id))
    return Extraction(
        docs=docs,
        outputs=outputs,
        cost_usd=ledger.total_usd,
        llm_calls=ledger.calls,
        page_text_s=page_text_s,
        extract_s=time.perf_counter() - t1,
        vision_pages=vision_pages,
        vision_usd=vision_usd,
    )


def _model_id() -> str:
    from creditpaper.config import extract_model

    provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower() or "anthropic"
    return f"{provider}:{extract_model()}"


# (path, document id) -> the verification committed for that fact.
Checks = dict[tuple[str, str], FactVerification]


def build_model(
    ext: Extraction,
    deal: str,
    deal_ccy: str | None = "AUD",
    checker_factory: CheckerFactory | None = None,
) -> tuple[DealModel, dict[str, int], Checks, set[str]]:
    """The production path: verify_and_build, then commit by concept (ingest.commit)."""
    model = DealModel(tenant_id="scorecard", deal_id=deal)
    stats = {"extractor_flags": 0, "apply_rejects": 0, "unmapped_refused": 0, "reconciliation_holds": 0}
    checks: Checks = {}
    unmapped: set[str] = set()
    model_id = _model_id()
    for d, out in ext.outputs:
        stats["extractor_flags"] += len(out.flags)
        pages = {int(p["page"]): str(p.get("text") or "") for p in d.pages}
        vision = frozenset(int(p["page"]) for p in d.pages if p.get("source") == "vision")
        facts = verify_and_build(
            out,
            pages,
            document_id=d.doc_id,
            deal_currency=deal_ccy,
            extractor_version=d.extractor_version or "unknown",
            model_id=model_id,
            vision_pages=vision,
            checker_factory=checker_factory,
        )
        for vf in facts:
            checks[(vf.path, vf.document_id)] = vf.verification
        result = commit(model, facts)
        for path, why in result.rejected.items():
            if why.startswith(f"{path}: unmapped"):
                unmapped.add(path)
                stats["unmapped_refused"] += 1
            else:
                stats["apply_rejects"] += 1
    dr = derive_economics(model)
    derive_metrics(model)
    derive_delivery_model(model)
    stats["reconciliation_holds"] += len(dr.inconsistent)
    try:
        from creditpaper.deals.utils.ingest import _reconcile

        stats["reconciliation_holds"] += len(_reconcile(model))
    except Exception:  # noqa: BLE001 - the scorecard does not depend on the named checks
        stats["reconciliation_holds"] = -1
    return model, stats, checks, unmapped


# -- verification ----------------------------------------------------------------


@dataclass
class Check:
    verdict: str
    reasons: list[str]
    page: int | None
    score: float | None
    confidence: float | None = None


# -- scoring ---------------------------------------------------------------------


@dataclass
class Row:
    path: str
    section: str
    category: str
    expected: str | None
    extracted: str | None
    correct: bool | None
    verdict: str
    reason: str
    page: int | None
    score: float | None
    slot: str = ""


def _fact_check(path: str, fact: Any, docs: dict[str, Doc], checks: Checks) -> tuple[str, Check | None, str]:
    """(anchor state, check, slot). anchor state: derived | unanchored:<why> | ok.

    The check is the verification committed on the fact (``Fact.verification``), with
    the anchor score from the same run."""
    if not isinstance(fact, Fact) or fact.provenance is None:
        return "unanchored:no_provenance", None, ""
    prov = fact.provenance
    doc = docs.get(prov.document_id)
    slot = doc.slot if doc else prov.document_id.split(":", 1)[0]
    if prov.method is ExtractionMethod.DERIVED:
        return "derived", None, slot
    quote = prov.location or ""
    if _PAGE_ONLY.match(quote):
        return "unanchored:no_clause", None, slot
    if doc is None or not doc.pages:
        return "unanchored:document_not_found", None, slot
    stored = fact.verification
    if stored is None:
        return "unanchored:not_verified", None, slot
    full = checks.get((path, prov.document_id))
    check = Check(
        stored.verdict,
        list(stored.reasons),
        full.page if full is not None else prov.page,
        round(full.anchor.score, 3) if full is not None else None,
        round(stored.confidence, 3),
    )
    return "ok", check, slot


def score(
    model: DealModel, gt: GroundTruth, ext: Extraction, checks: Checks, unmapped: set[str] | None = None
) -> list[Row]:
    docs = {d.doc_id: d for d in ext.docs}
    rows: list[Row] = []
    seen: set[str] = set()
    for line in reconcile(model, gt).lines:
        if line.verdict is Verdict.UNMODELLED:
            continue
        seen.add(line.path)
        correct = line.verdict in (Verdict.MATCH, Verdict.EXPLAINED)
        base = {
            "path": line.path,
            "section": line.section,
            "expected": line.expected,
            "extracted": line.actual,
        }
        if line.actual is None:  # MISSING, or EXPLAINED-missing (ground truth says why)
            reason = "missing_explained" if correct else "missing"
            rows.append(
                Row(
                    **base,
                    category="missing",
                    correct=correct,
                    verdict="",
                    reason=reason,
                    page=None,
                    score=None,
                )
            )
            continue
        state, check, slot = _fact_check(line.path, resolve_path(model, line.path), docs, checks)
        if check is None:
            cat = "derived" if state == "derived" else "unanchored"
            reason = state if cat == "unanchored" else ("derived_correct" if correct else "derived_wrong")
            rows.append(
                Row(
                    **base,
                    category=cat,
                    correct=correct,
                    verdict="",
                    reason=reason,
                    page=None,
                    score=None,
                    slot=slot,
                )
            )
            continue
        ok = check.verdict in _VERIFIED
        cat = ("correct_verified" if ok else "false_flag") if correct else ("FALSE_PASS" if ok else "catch")
        rows.append(
            Row(
                **base,
                category=cat,
                correct=correct,
                verdict=check.verdict,
                reason="|".join(check.reasons),
                page=check.page,
                score=check.score,
                slot=slot,
            )
        )
    # every other extracted fact: verified or not, no ground truth to judge it by
    for _, out in ext.outputs:
        for path in sorted(out.fields):
            if path in seen:
                continue
            seen.add(path)
            fact = resolve_path(model, path)
            val = fact.value if isinstance(fact, Fact) else None
            state, check, slot = _fact_check(path, fact, docs, checks)
            if path in (unmapped or set()):
                state, check = "unmapped", None
            rows.append(
                Row(
                    path=path,
                    section="not_in_ground_truth",
                    category="not_in_ground_truth",
                    expected=None,
                    extracted=None if val is None else str(val),
                    correct=None,
                    verdict=check.verdict if check else state,
                    reason="|".join(check.reasons) if check else state,
                    page=check.page if check else None,
                    score=check.score if check else None,
                    slot=slot,
                )
            )
    return rows


def _group(path: str) -> str:
    return re.sub(r"\[[^\]]*\]", "[]", path).rsplit(".", 1)[0] if "." in path else path


def failure_reasons(rows: list[Row]) -> Counter[str]:
    c: Counter[str] = Counter()
    for r in rows:
        if r.category in ("correct_verified", "not_in_ground_truth") or (
            r.category == "derived" and r.correct
        ):
            continue
        if r.category == "missing":
            if r.reason == "missing":
                c[f"missing:{_group(r.path)}"] += 1
        elif r.category in ("unanchored", "derived"):
            c[r.reason if r.reason.startswith(r.category) else f"{r.category}:{r.reason}"] += 1
        elif r.category == "FALSE_PASS":  # verified, so there is no reason: name the field group
            c[f"FALSE_PASS:{_group(r.path)}"] += 1
        else:
            for reason in (r.reason or r.verdict).split("|"):
                c[f"{r.category}:{reason or r.verdict}"] += 1
    return c


# -- label store as the answer key ------------------------------------------------


def parse_labels_arg(spec: str) -> dict[str, str]:
    """`tenant=<uuid>[,deal=<id>][,extractor=<v>]` -> its parts. tenant is required."""
    parts: dict[str, str] = {}
    for item in spec.split(","):
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in ("tenant", "deal", "extractor") or not value:
            raise SystemExit(f"--labels: expected tenant=<uuid>[,deal=<id>][,extractor=<v>], got {item!r}")
        parts[key] = value
    if "tenant" not in parts:
        raise SystemExit("--labels needs tenant=<uuid>")
    from creditpaper.db import tenant_uuid

    try:
        tenant_uuid(parts["tenant"])
    except ValueError:
        raise SystemExit("--labels: tenant must be a uuid") from None
    return parts


def _latest(labels: list[Any]) -> dict[tuple[str, str], Any]:
    """The newest label per (deal, concept): a later decision supersedes an earlier one."""
    latest: dict[tuple[str, str], Any] = {}
    for label in sorted(labels, key=lambda lb: (lb.decided_at, lb.id or "")):
        latest[(label.deal_id, label.concept_path)] = label
    return latest


def labels_answer_key(labels: list[Any], deal_name: str) -> GroundTruth:
    """A GroundTruth built from labels, so reconcile() scores an extraction run against
    analyst decisions. Numbers go to figures_numeric (precision-aware match), anything
    else to reference_values. A concept set aside with no final value has no answer."""
    from creditpaper.ledger.labels import as_decimal

    numeric: dict[str, str] = {}
    reference: dict[str, str] = {}
    explainable: dict[str, str] = {}
    for (_, path), label in sorted(_latest(labels).items()):
        final = label.final_value
        if final is None:
            continue
        text = final if isinstance(final, str) else json.dumps(final, default=str)
        (numeric if as_decimal(final) is not None else reference)[path] = text
        if label.reason_code == "explainable_difference":
            explainable[path] = "seed: known difference from the documents"
    return GroundTruth(
        deal_name=deal_name,
        figures_numeric=numeric,
        derived_expected={},
        computed_ratios={},
        reference_values=reference,
        explainable=explainable,
    )


def score_labels(labels: list[Any]) -> dict[str, list[Row]]:
    """Score recorded analyst decisions per deal: the scorecard categories, from use."""
    from creditpaper.ledger.labels import values_agree

    out: dict[str, list[Row]] = {}
    analyst = [lb for lb in labels if lb.source == "analyst"]
    for (deal, path), label in sorted(_latest(analyst).items()):
        extracted, final = label.extracted_value, label.final_value
        if extracted is None:
            category, correct = "missing", False  # the analyst supplied what the system did not
        elif final is not None and values_agree(extracted, final):
            category, correct = ("correct_verified" if label.passed else "false_flag"), True
        else:  # corrected, another source chosen, or set aside with no replacement
            category, correct = ("FALSE_PASS" if label.passed else "catch"), False
        decision_reason = label.action + (f":{label.reason_code}" if label.reason_code else "")
        review_reasons = tuple(label.verify_reasons or ())
        reason = "|".join((decision_reason, *review_reasons))
        out.setdefault(deal, []).append(
            Row(
                path=path,
                section="labels",
                category=category,
                expected=None if final is None else str(final),
                extracted=None if extracted is None else str(extracted),
                correct=correct,
                verdict=label.verdict_at_decision or "",
                reason="missing" if category == "missing" else reason,
                page=label.page,
                score=None,
                slot=label.extractor_version or "",
            )
        )
    return out


def run_labels(
    parts: dict[str, str],
    *,
    out_dir: Path,
    quiet: bool = False,
    fetch: Callable[..., list[Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Label-only mode: score each labelled deal's decisions; one scorecard per deal."""
    if fetch is None:
        from creditpaper.ledger.labels import labels_for

        fetch = labels_for
    labels = fetch(parts["tenant"], deal_id=parts.get("deal"), extractor_version=parts.get("extractor"))
    empty = Extraction(docs=[], outputs=[])
    stats = {"extractor_flags": 0, "apply_rejects": 0, "reconciliation_holds": 0}
    timings = {"page_text": 0.0, "extract": 0.0, "verify": 0.0, "total": 0.0}
    results: dict[str, dict[str, Any]] = {}
    for deal, rows in score_labels(labels).items():
        summary = summarise(rows, empty, stats, timings)
        summary["answer_key"] = "labels"
        top = failure_reasons(rows).most_common(10)
        out = write_outputs(out_dir, deal, rows, summary, top)
        if not quiet:
            print_summary(deal, summary, top, out)
        results[deal] = {"summary": summary, "top": top, "rows": rows, "out": out}
    if not results and not quiet:
        print("no analyst labels for this tenant and filter")
    return results


# -- output ----------------------------------------------------------------------


def summarise(
    rows: list[Row], ext: Extraction, stats: dict[str, int], timings: dict[str, float]
) -> dict[str, Any]:
    gt_rows = [r for r in rows if r.category != "not_in_ground_truth"]
    counts = Counter(r.category for r in gt_rows)
    cv, ff, ca, fp = (counts[k] for k in ("correct_verified", "false_flag", "catch", "FALSE_PASS"))
    all_rows = [r for r in rows if r.verdict]
    verdicts = Counter(
        r.verdict
        for r in rows
        if r.category != "missing" and r.verdict and not r.verdict.startswith(("unanch", "deriv"))
    )

    def rate(n: int, d: int) -> float | None:
        return round(n / d, 3) if d else None

    correct_total = sum(1 for r in gt_rows if r.correct)
    family_rows: dict[str, list[Row]] = defaultdict(list)
    for row in gt_rows:
        family_rows[re.split(r"[.\[]", row.path, maxsplit=1)[0] or "other"].append(row)
    false_flag_reasons = Counter(
        reason
        for row in gt_rows
        if row.category == "false_flag"
        for reason in (row.reason.split("|")[1:] or ["UNCLASSIFIED_LEGACY"])
    )
    return {
        "scorecard_version": SCORECARD_VERSION,
        "gt_fields": len(gt_rows),
        "categories": {k: counts.get(k, 0) for k in CATEGORIES},
        "derived_correct": sum(1 for r in gt_rows if r.category == "derived" and r.correct),
        "correct_total": correct_total,
        "rates": {
            "accuracy": rate(correct_total, len(gt_rows)),
            "accuracy_confidence_95_lower": wilson_lower_bound(correct_total, len(gt_rows)),
            "false_pass_rate": rate(fp, cv + fp),  # of verified facts, share that are wrong
            "catch_rate": rate(ca, ca + fp),  # of wrong anchored facts, share flagged
            "false_flag_rate": rate(ff, ff + cv),  # of correct anchored facts, share flagged
            "unclassified_false_flag_rate": rate(false_flag_reasons.get("UNCLASSIFIED_LEGACY", 0), ff),
        },
        "all_facts": len(rows) - counts.get("missing", 0),
        "all_facts_verdicts": dict(verdicts.most_common()),
        "checked_facts": len(all_rows),
        "concept_families": {
            family: {
                "fields": len(items),
                "correct": sum(1 for item in items if item.correct),
            }
            for family, items in sorted(family_rows.items())
        },
        "false_flag_reasons": dict(false_flag_reasons.most_common()),
        "docs": dict(Counter(d.source.split(":")[0] for d in ext.docs)),
        "docs_by_slot": dict(Counter(d.slot for d in ext.docs)),
        **stats,
        "llm_cost_usd": round(ext.cost_usd, 4),
        "llm_calls": ext.llm_calls,
        "vision_pages": ext.vision_pages,
        "vision_usd": round(ext.vision_usd, 4),
        "vision_text_facts": sum(1 for r in rows if "vision_text" in r.reason.split("|")),
        "timings_s": {k: round(v, 2) for k, v in timings.items()},
    }


def wilson_lower_bound(successes: int, total: int, *, z: float = 1.959963984540054) -> float | None:
    """Conservative binomial accuracy bound; unlike a raw rate it penalises tiny samples."""
    if total <= 0:
        return None
    proportion = successes / total
    z2 = z * z
    centre = proportion + z2 / (2 * total)
    margin = z * math.sqrt((proportion * (1 - proportion) + z2 / (4 * total)) / total)
    return round((centre - margin) / (1 + z2 / total), 4)


def aggregate_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine independent deal scorecards into one release-grade quality gate."""
    fields = sum(int(item["gt_fields"]) for item in summaries)
    correct = sum(int(item["correct_total"]) for item in summaries)
    categories = {
        category: sum(int(item["categories"].get(category, 0)) for item in summaries)
        for category in CATEGORIES
    }
    verified = categories["correct_verified"] + categories["FALSE_PASS"]
    families: dict[str, dict[str, int]] = defaultdict(lambda: {"fields": 0, "correct": 0})
    false_flag_reasons: Counter[str] = Counter()
    for item in summaries:
        for family, counts in item.get("concept_families", {}).items():
            families[family]["fields"] += int(counts["fields"])
            families[family]["correct"] += int(counts["correct"])
        false_flag_reasons.update(item.get("false_flag_reasons", {}))
    correct_anchored = categories["correct_verified"] + categories["false_flag"]
    return {
        "deals": len(summaries),
        "gt_fields": fields,
        "correct_total": correct,
        "categories": categories,
        "rates": {
            "accuracy": round(correct / fields, 3) if fields else None,
            "accuracy_confidence_95_lower": wilson_lower_bound(correct, fields),
            "false_pass_rate": (round(categories["FALSE_PASS"] / verified, 3) if verified else None),
            "false_flag_rate": (
                round(categories["false_flag"] / correct_anchored, 3) if correct_anchored else None
            ),
            "unclassified_false_flag_rate": (
                round(false_flag_reasons.get("UNCLASSIFIED_LEGACY", 0) / categories["false_flag"], 3)
                if categories["false_flag"]
                else 0.0
            ),
        },
        "concept_families": dict(sorted(families.items())),
        "false_flag_reasons": dict(false_flag_reasons.most_common()),
        "timings_s": {"total": sum(float(item["timings_s"]["total"]) for item in summaries)},
        "performance_measured": all(
            bool(item.get("performance_measured", float(item["timings_s"]["total"]) > 0))
            for item in summaries
        ),
    }


def write_outputs(
    out_dir: Path, deal: str, rows: list[Row], summary: dict[str, Any], top: list[tuple[str, int]]
) -> Path:
    d = out_dir / deal
    d.mkdir(parents=True, exist_ok=True)
    with (d / "scorecard.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(asdict(rows[0]).keys()) if rows else ["path"])
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))
    payload = {
        "deal": deal,
        "summary": summary,
        "top_failure_reasons": top,
        "facts": [asdict(r) for r in rows],
    }
    (d / "scorecard.json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    stamp = datetime.now().isoformat(timespec="seconds")
    with (out_dir / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": stamp, "deal": deal, **summary}) + "\n")
    return d


def print_summary(deal: str, s: dict[str, Any], top: list[tuple[str, int]], out: Path) -> None:
    c, rt = s["categories"], s["rates"]
    print(f"\n=== INGEST SCORECARD: {deal} ===")
    print(f"docs {sum(s['docs'].values())} {s['docs']}  by slot {s['docs_by_slot']}")
    print(
        f"ground-truth fields: {s['gt_fields']}   correct: {s['correct_total']}   accuracy: {rt['accuracy']}"
    )
    labels = {
        "correct_verified": "correct + verified",
        "false_flag": "correct + flagged (false flag)",
        "catch": "wrong + flagged (catch)",
        "FALSE_PASS": "WRONG + VERIFIED (FALSE PASS)",
        "missing": "missing",
        "unanchored": "unanchored",
        "derived": f"derived (no quote; {s['derived_correct']} correct)",
    }
    for k in CATEGORIES:
        print(f"  {labels[k]:<34} {c[k]:>4}")
    print(
        f"rates: false-pass {rt['false_pass_rate']}  catch {rt['catch_rate']}  "
        f"false-flag {rt['false_flag_rate']}"
    )
    print(f"all extracted facts: {s['all_facts']}  verdicts {s['all_facts_verdicts']}")
    print(
        f"extractor flags {s['extractor_flags']}  apply rejects {s['apply_rejects']}  "
        f"unmapped refused {s.get('unmapped_refused', 0)}  "
        f"reconciliation holds {s['reconciliation_holds']}"
    )
    t = s["timings_s"]
    print(
        f"time: total {t['total']}s (page text {t['page_text']}s, extract {t['extract']}s, "
        f"verify {t['verify']}s)   LLM ${s['llm_cost_usd']:.4f} over {s['llm_calls']} calls"
    )
    if s["vision_pages"]:
        print(
            f"vision: {s['vision_pages']} transcribed page(s), ${s['vision_usd']:.4f} this run, "
            f"{s['vision_text_facts']} fact(s) checked on vision text (capped for review)"
        )
    print("top failure reasons:")
    for reason, n in top:
        print(f"  {n:>4}  {reason}")
    print(f"detail: {out}")


def target_failures(
    summary: dict[str, Any],
    *,
    min_accuracy: float | None,
    max_total_seconds: float | None,
    max_false_pass_rate: float | None,
    min_ground_truth_fields: int,
    min_accuracy_confidence_lower: float | None = None,
    max_false_flag_rate: float | None = None,
    max_unclassified_false_flag_rate: float | None = None,
    min_deals: int = 0,
    min_concept_families: int = 0,
) -> list[str]:
    """Return deterministic SLA/quality gate failures for one scorecard."""
    failures: list[str] = []
    fields = int(summary["gt_fields"])
    accuracy = summary["rates"]["accuracy"]
    false_pass = summary["rates"]["false_pass_rate"]
    total = float(summary["timings_s"]["total"])
    performance_measured = bool(summary.get("performance_measured", total > 0))
    confidence_lower = summary["rates"].get("accuracy_confidence_95_lower")
    false_flag = summary["rates"].get("false_flag_rate")
    unclassified_false_flag = summary["rates"].get("unclassified_false_flag_rate")
    deals = int(summary.get("deals", 1))
    families = len(summary.get("concept_families", {}))
    if deals < min_deals:
        failures.append(f"labelled deals {deals} < {min_deals}")
    if families < min_concept_families:
        failures.append(f"concept families {families} < {min_concept_families}")
    if fields < min_ground_truth_fields:
        failures.append(f"ground-truth fields {fields} < {min_ground_truth_fields}")
    if min_accuracy is not None and (accuracy is None or accuracy < min_accuracy):
        failures.append(f"accuracy {accuracy} < {min_accuracy}")
    if min_accuracy_confidence_lower is not None and (
        confidence_lower is None or confidence_lower < min_accuracy_confidence_lower
    ):
        failures.append(
            f"95% accuracy confidence lower bound {confidence_lower} < {min_accuracy_confidence_lower}"
        )
    if max_total_seconds is not None:
        if not performance_measured:
            failures.append("ingestion performance evidence missing")
        elif total > max_total_seconds:
            failures.append(f"total time {total}s > {max_total_seconds}s")
    if max_false_pass_rate is not None and (false_pass is None or false_pass > max_false_pass_rate):
        failures.append(f"false-pass rate {false_pass} > {max_false_pass_rate}")
    if max_false_flag_rate is not None and (false_flag is None or false_flag > max_false_flag_rate):
        failures.append(f"false-flag rate {false_flag} > {max_false_flag_rate}")
    if max_unclassified_false_flag_rate is not None and (
        unclassified_false_flag is None or unclassified_false_flag > max_unclassified_false_flag_rate
    ):
        failures.append(
            f"unclassified false-flag rate {unclassified_false_flag} > {max_unclassified_false_flag_rate}"
        )
    return failures


def write_release_verdict(out_dir: Path, summary: dict[str, Any], failures: list[str]) -> None:
    """Machine-readable release evidence; aggregate only, never document values or source text."""
    payload = {
        "scorecard_version": SCORECARD_VERSION,
        "passed": not failures,
        "failures": failures,
        "summary": summary,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "release-verdict.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_performance_evidence(path: Path, *, min_documents: int = 50) -> dict[str, Any]:
    """Validate a real scorecard artifact before using it as SLA evidence."""
    raw = path.read_bytes()
    evidence = json.loads(raw)
    measured = evidence.get("summary", evidence)
    measured_total = float(measured.get("timings_s", {}).get("total", 0))
    document_count = sum(int(count) for count in measured.get("docs", {}).values())
    if measured_total <= 0:
        raise ValueError("performance summary has no positive timings_s.total")
    if document_count < min_documents:
        raise ValueError(
            f"performance summary has {document_count} documents; at least {min_documents} required"
        )
    if measured.get("answer_key") == "labels" and document_count == 0:
        raise ValueError("label-only scorecards are not performance evidence")
    return {
        "total_seconds": measured_total,
        "documents": document_count,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def run(
    ddir: Path,
    *,
    cache: Path,
    out_dir: Path,
    no_llm: bool = False,
    deal_currency: str | None = "AUD",
    registry: Mapping[str, type] | None = None,
    quiet: bool = False,
    vision: bool = False,
    vision_slots: frozenset[str] | None = None,
    vision_budget_usd: float = 0.0,
    vision_client: Any = None,
    answer_key: GroundTruth | None = None,
    support: bool = False,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    log: Callable[[str], None] = (lambda _m: None) if quiet else print
    ext = run_extraction(
        ddir,
        cache,
        no_llm=no_llm,
        registry=registry,
        log=log,
        vision=vision,
        vision_slots=vision_slots,
        vision_budget_usd=vision_budget_usd,
        vision_client=vision_client,
    )
    t_model = time.perf_counter()
    model, stats, checks, unmapped = build_model(
        ext, ddir.name, deal_currency, default_checker_factory if support else None
    )
    gt = answer_key or GroundTruth.load(ddir / "ground_truth.json")
    rows = score(model, gt, ext, checks, unmapped)
    t_verify = time.perf_counter() - t_model
    timings = {
        "page_text": ext.page_text_s,
        "extract": ext.extract_s,
        "verify": t_verify,
        "total": time.perf_counter() - t0,
    }
    summary = summarise(rows, ext, stats, timings)
    top = failure_reasons(rows).most_common(10)
    out = write_outputs(out_dir, ddir.name, rows, summary, top)
    if not quiet:
        print_summary(ddir.name, summary, top, out)
    return {"summary": summary, "top": top, "rows": rows, "out": out}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--deal", default=None, help="build_deal, blind_01, ... or a deal folder (default build_deal)"
    )
    ap.add_argument(
        "--labels",
        default=None,
        metavar="tenant=<uuid>[,deal=<id>][,extractor=<v>]",
        help="answer key from the tenant's label store instead of ground_truth.json",
    )
    ap.add_argument("--fixtures", type=Path, default=None)
    ap.add_argument(
        "--out", type=Path, default=None, help="output dir (default: <tmp>/creditpaper_scorecard)"
    )
    ap.add_argument("--cache", type=Path, default=None, help="cache dir (default: <out>/cache)")
    ap.add_argument(
        "--performance-summary",
        type=Path,
        default=None,
        help="measured ingestion scorecard JSON used to prove the production time target",
    )
    ap.add_argument("--no-llm", action="store_true", help="use cached extractions only")
    ap.add_argument(
        "--deal-currency", default="AUD", help="analyst-confirmed deal currency ('' = unconfirmed)"
    )
    ap.add_argument(
        "--vision", action="store_true", help="transcribe text-less pages (spends; see docstring)"
    )
    ap.add_argument("--vision-slots", default="", help="comma-separated slots to transcribe ('' = all)")
    ap.add_argument(
        "--vision-budget-usd", type=float, default=0.0, help="refuse a vision run estimated above this"
    )
    ap.add_argument(
        "--support",
        action="store_true",
        help="run the semantic support check on material facts (spends; off by default)",
    )
    ap.add_argument("--min-accuracy", type=float, default=None)
    ap.add_argument(
        "--production-gate",
        action="store_true",
        help=(
            "enforce the release targets: accuracy >=95%%, Wilson 95%% lower bound >=95%%, "
            "false-pass rate <=1%%, at least 73 labelled fields, and <=240 seconds"
        ),
    )
    ap.add_argument("--max-total-seconds", type=float, default=None)
    ap.add_argument("--max-false-pass-rate", type=float, default=None)
    ap.add_argument("--max-false-flag-rate", type=float, default=None)
    ap.add_argument("--max-unclassified-false-flag-rate", type=float, default=None)
    ap.add_argument("--min-ground-truth-fields", type=int, default=1)
    ap.add_argument("--min-labelled-deals", type=int, default=1)
    ap.add_argument("--min-concept-families", type=int, default=1)
    ap.add_argument(
        "--min-accuracy-confidence-lower",
        type=float,
        default=None,
        help="require the Wilson 95%% lower confidence bound to meet this value",
    )
    args = ap.parse_args(argv)
    if args.production_gate:
        defaults = {
            "min_accuracy": 0.95,
            "min_accuracy_confidence_lower": 0.95,
            "max_false_pass_rate": 0.01,
            "max_false_flag_rate": 0.20,
            "max_unclassified_false_flag_rate": 0.05,
            "min_ground_truth_fields": 73,
            "min_labelled_deals": 3,
            "min_concept_families": 5,
            "max_total_seconds": 240.0,
        }
        for name, value in defaults.items():
            if (
                getattr(args, name) is None
                or name
                in {
                    "min_ground_truth_fields",
                    "min_labelled_deals",
                    "min_concept_families",
                }
                and getattr(args, name) == 1
            ):
                setattr(args, name, value)
    for name in (
        "min_accuracy",
        "max_false_pass_rate",
        "max_false_flag_rate",
        "max_unclassified_false_flag_rate",
        "min_accuracy_confidence_lower",
    ):
        value = getattr(args, name)
        if value is not None and not 0 <= value <= 1:
            ap.error(f"--{name.replace('_', '-')} must be between 0 and 1")
    if args.max_total_seconds is not None and args.max_total_seconds <= 0:
        ap.error("--max-total-seconds must be positive")
    if args.min_ground_truth_fields < 1:
        ap.error("--min-ground-truth-fields must be positive")
    if args.min_labelled_deals < 1 or args.min_concept_families < 1:
        ap.error("coverage minimums must be positive")

    fixtures = args.fixtures or default_fixtures()
    try:
        from dotenv import load_dotenv

        for env in (ROOT / ".env", fixtures.parent / ".env"):
            if env.exists():
                load_dotenv(env)
                break
    except ImportError:
        pass
    out_dir = args.out or Path(
        os.getenv("CREDITPAPER_SCORECARD_OUT", Path(tempfile.gettempdir()) / "creditpaper_scorecard")
    )
    cache = args.cache or out_dir / "cache"
    parts = parse_labels_arg(args.labels) if args.labels else None
    if parts is not None and args.deal is None:
        results = run_labels(parts, out_dir=out_dir)
        aggregate = aggregate_summaries([result["summary"] for result in results.values()])
        if args.performance_summary:
            try:
                performance = load_performance_evidence(args.performance_summary)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                ap.error(f"invalid --performance-summary: {exc}")
            aggregate["timings_s"]["total"] = performance["total_seconds"]
            aggregate["performance_measured"] = True
            aggregate["performance_evidence"] = performance
        (out_dir / "aggregate.json").write_text(json.dumps(aggregate, indent=2), encoding="utf-8")
        failures = target_failures(
            aggregate,
            min_accuracy=args.min_accuracy,
            max_total_seconds=args.max_total_seconds,
            max_false_pass_rate=args.max_false_pass_rate,
            min_ground_truth_fields=args.min_ground_truth_fields,
            min_accuracy_confidence_lower=args.min_accuracy_confidence_lower,
            max_false_flag_rate=args.max_false_flag_rate,
            max_unclassified_false_flag_rate=args.max_unclassified_false_flag_rate,
            min_deals=args.min_labelled_deals,
            min_concept_families=args.min_concept_families,
        )
        write_release_verdict(out_dir, aggregate, failures)
        if failures:
            print(f"scorecard targets failed: {'; '.join(failures)}")
            return 2
        return 0
    answer_key = None
    if parts is not None:
        if "deal" not in parts:
            raise SystemExit("--deal with --labels needs deal=<id>: which labelled deal is the answer key")
        from creditpaper.ledger.labels import labels_for

        answer_key = labels_answer_key(labels_for(parts["tenant"], deal_id=parts["deal"]), parts["deal"])
    if not args.no_llm and not os.getenv("ANTHROPIC_API_KEY") and not os.getenv("LLM_PROVIDER"):
        print("no ANTHROPIC_API_KEY: running --no-llm on cached extractions only")
        args.no_llm = True
    selected_deal = deal_dir(fixtures, args.deal or "build_deal", need_ground_truth=answer_key is None)
    if (
        args.production_gate
        and args.labels is None
        and "blind" not in {part.casefold() for part in selected_deal.resolve().parts}
    ):
        raise SystemExit("--production-gate fixture runs require a deal under fixtures/blind")
    result = run(
        selected_deal,
        cache=cache,
        out_dir=out_dir,
        no_llm=args.no_llm,
        deal_currency=args.deal_currency or None,
        vision=args.vision,
        vision_slots=frozenset(s for s in args.vision_slots.split(",") if s) or None,
        vision_budget_usd=args.vision_budget_usd,
        answer_key=answer_key,
        support=args.support,
    )
    failures = target_failures(
        result["summary"],
        min_accuracy=args.min_accuracy,
        max_total_seconds=args.max_total_seconds,
        max_false_pass_rate=args.max_false_pass_rate,
        min_ground_truth_fields=args.min_ground_truth_fields,
        min_accuracy_confidence_lower=args.min_accuracy_confidence_lower,
        max_false_flag_rate=args.max_false_flag_rate,
        max_unclassified_false_flag_rate=args.max_unclassified_false_flag_rate,
        min_deals=args.min_labelled_deals,
        min_concept_families=args.min_concept_families,
    )
    write_release_verdict(out_dir, result["summary"], failures)
    if failures:
        print(f"scorecard targets failed: {'; '.join(failures)}")
        return 2
    return 0


if __name__ == "__main__":
    import logging

    # Per-stage timing lines drown the scorecard on a terminal. Muted here, not in
    # main(), so a caller importing main() keeps its own logging configuration.
    logging.getLogger("creditpaper.obs.timing").setLevel(logging.WARNING)
    raise SystemExit(main())
