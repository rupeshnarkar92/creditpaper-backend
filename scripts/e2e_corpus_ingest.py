"""End to end through the PRODUCTION path, on real deal files.

The only check that runs the whole chain as a deal really runs it. Every other
measurement in `scripts/` calls `extract_corpus` directly and therefore proves
nothing about the ledger commit, the slot taxonomy, `_primary_blocks`, the
support triage or the job wiring. This proved the wiring worked the first time
it was run (2026-09-25), which is also the first time the wired path had ever
been executed at all.

NOT a pytest test, deliberately: it needs a database, real fixture documents and
a model call, and `tests/` is guaranteed to need none of those. Run it by hand
before shipping a change to the ingestion path.

REQUIRES: DATABASE_URL, an API key in .env, and `fixtures/` (tenant data, so
this is a checkout-only script and prints no document text).

The chain:

    documents + versions  ->  process_version (parse, with OCR)
                          ->  a confirmed slot per document (D3)
                          ->  run_corpus_extraction_pipeline
                          ->  ingest_corpus -> apply_outputs -> commit_outputs
                          ->  load_model, and read the facts back out

so it exercises the ledger commit, the slot taxonomy, `_primary_blocks`, the
support triage and the job wiring — none of which a direct engine call touches.

Small BY DEFAULT: a handful of documents, so a failure is legible and the spend
is a few cents. `--limit 0` runs the whole folder, which is the shape of a
production folder drop and the only way to measure the time budget.

Every run is genuinely cold. The deal id is new, and the fact cache is keyed on
`(tenant_id, deal_id, content_hash, prompt_version, model_id)`, so a new deal
cannot hit a previous run's draws however many times this is run.
"""

import argparse
import hashlib
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "tests")
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(".env"))

from conftest_helper_deals import seed_deal  # noqa: E402
from creditpaper.concurrency import pool  # noqa: E402
from creditpaper.db import cursor  # noqa: E402

TENANT = "00000000-0000-0000-0000-000000000001"
HOW_MANY = 6
# Production parses each document as its own independent ledger job, so they
# overlap. Parsing them one after another here would measure a pipeline nobody
# runs — and would hide the whole cost of the OCR tail behind a serial clock.
PARSE_WORKERS = 8
ACTOR = str(uuid.uuid4())

# Everything the corpus path can read. Restricting this to PDFs understated the
# corpus: a folder drop is whatever the analyst dragged in. The media type is
# what routes the parse, so it has to be the real one — attaching a .docx as
# `application/pdf` sends it to the PDF extractor, which reads nothing and says
# nothing about it.
_READABLE = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def pick_documents(folder: Path, limit: int) -> list[Path]:
    """Real deal files, smallest first so a truncated run is still quick."""
    found = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in _READABLE]
    ordered = sorted(found, key=lambda p: p.stat().st_size)
    return ordered if limit <= 0 else ordered[:limit]


def attach(deal_id: str, path: Path) -> tuple[str, str]:
    """One `deal_documents` row and one version, pointing at the real file."""
    doc_id, version_id = str(uuid.uuid4()), str(uuid.uuid4())
    raw = path.read_bytes()
    with cursor() as cur:
        cur.execute(
            """INSERT INTO deal_documents
                 (id, tenant_id, deal_id, display_name, document_kind, created_by)
               VALUES (%s, %s, %s, %s, 'other', %s)""",
            (doc_id, TENANT, deal_id, path.name, ACTOR),
        )
        cur.execute(
            """INSERT INTO deal_document_versions
                 (id, tenant_id, deal_id, document_id, version_number, storage_path,
                  original_filename, mime_type, size_bytes, content_sha256, uploaded_by)
               VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s)""",
            (
                version_id,
                TENANT,
                deal_id,
                doc_id,
                str(path.resolve()),
                path.name,
                _READABLE[path.suffix.lower()],
                len(raw),
                hashlib.sha256(raw).hexdigest(),
                str(uuid.uuid4()),
            ),
        )
    return doc_id, version_id


def make_slot(deal_id: str, slot_key: str) -> str:
    """One CONFIRMED slot. `uq_document_slots_deal_slot` allows exactly one per
    key per deal, which is right: a deal has one valuation slot that may hold
    several files."""
    slot_id = str(uuid.uuid4())
    with cursor() as cur:
        cur.execute(
            """INSERT INTO document_slots (id, tenant_id, deal_id, slot_key, status)
               VALUES (%s, %s, %s, %s, 'confirmed')""",
            (slot_id, TENANT, deal_id, slot_key),
        )
    return slot_id


def place(slot_id: str, doc_id: str, version_id: str) -> None:
    """Assign a document to the slot — D3's precondition for reading it."""
    with cursor() as cur:
        cur.execute(
            """INSERT INTO document_slot_files
                 (tenant_id, slot_id, document_id, version_id, role)
               VALUES (%s, %s, %s, %s, 'assigned')""",
            (TENANT, slot_id, doc_id, version_id),
        )


def parsed_pages(deal_id: str) -> tuple[int, int]:
    """How many pages were parsed, and how many of them had to be OCR'd.

    Read back out of the database rather than counted in the parse, because
    what the extraction reads is what the database holds — a page the parse
    produced and failed to store is a page the deal does not have.
    """
    with cursor() as cur:
        cur.execute(
            "SELECT page_texts FROM deal_document_extractions WHERE tenant_id = %s AND deal_id = %s",
            (TENANT, deal_id),
        )
        rows = cur.fetchall()
    pages = [p for (texts,) in rows for p in (texts or [])]
    return len(pages), sum(1 for p in pages if p.get("ocr_used"))


def main() -> int:
    from creditpaper.deals.services.workspace import run_corpus_extraction_pipeline
    from creditpaper.ledger.service import load_model
    from creditpaper.processing.pipeline import process_version

    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default="fixtures/blind/blind_03/deal_files/RFI")
    ap.add_argument("--limit", type=int, default=HOW_MANY, help="0 for the whole folder")
    ap.add_argument("--parse-workers", type=int, default=PARSE_WORKERS)
    ap.add_argument("--passes", type=int, default=None)
    args = ap.parse_args()

    files = pick_documents(Path(args.folder), args.limit)
    if not files:
        print(f"FAIL: no readable documents under {args.folder}")
        return 1
    deal_id = seed_deal(name="E2E corpus", tenant_id=TENANT)
    print(f"deal {deal_id}, {len(files)} documents from {args.folder}")

    slot_id = make_slot(deal_id, "valuation")
    attached = [attach(deal_id, path) for path in files]

    started = time.perf_counter()
    with pool(max(1, args.parse_workers), thread_name_prefix="e2e-parse") as executor:
        list(executor.map(lambda pair: process_version(pair[1]), attached))
    parse_s = time.perf_counter() - started
    for doc_id, version_id in attached:
        place(slot_id, doc_id, version_id)
    pages, ocr = parsed_pages(deal_id)
    print(f"parsed and placed        {parse_s:6.1f}s  ({pages} pages, {ocr} OCR'd)")

    # The spend comes off the REPORT, not off a session opened around the call.
    # `ingest_corpus` opens its own `cost_session`, so an outer one sees an empty
    # ledger and prints $0.00 for a run that really cost money — which is how
    # this script first reported a 33s extraction as free.
    started = time.perf_counter()
    report = run_corpus_extraction_pipeline(
        deal_id, str(uuid.uuid4()), TENANT, **({"passes": args.passes} if args.passes else {})
    )
    extract_s = time.perf_counter() - started
    if report is None:
        print("FAIL: the pipeline found nothing to read")
        return 1
    spend = report.cost_usd

    # A cost that cannot be trusted has to SAY so: $0.00 looks identical whether
    # the run was free or the calls escaped the ledger.
    from creditpaper.obs.ledger import unscoped_call_count

    escaped = unscoped_call_count()
    caveat = f"  UNSCOPED: {escaped} calls escaped the ledger" if escaped else ""
    print(f"corpus extraction        {extract_s:6.1f}s  (${spend:.2f}){caveat}")
    print(f"INGESTION TOTAL          {parse_s + extract_s:6.1f}s  (${spend:.2f})")
    if report.failed_documents:
        print(f"  documents that FAILED  {report.failed_documents}")
    if report.reconciliation:
        print(f"  cross-document flags   {len(report.reconciliation)}")

    model = load_model(TENANT, deal_id)
    if model is None:
        print("FAIL: no model was committed")
        return 1

    from creditpaper.config import EXTRACTION_CONFIDENCE_THRESHOLD
    from creditpaper.deals.services.fact_walk import walk_facts

    facts = list(walk_facts(model.model_dump(mode="json")))
    stated = [(p, f) for p, f in facts if f.get("value") is not None]
    anchored = [(p, f) for p, f in stated if (f.get("provenance") or {}).get("location")]
    print(f"\nfacts with a value       {len(stated)}")
    print(f"  of those, anchored     {len(anchored)}")
    by_status: dict[str, int] = {}
    for _p, f in stated:
        by_status[str(f.get("status"))] = by_status.get(str(f.get("status")), 0) + 1
    for status, n in sorted(by_status.items()):
        print(f"  {status:<12} {n}")

    # The threshold is what decides whether a fact is usable without an analyst
    # looking at it, so it is the accuracy number that means something. Below it
    # is not "wrong" — it is "say so, and make someone read the page".
    confident = [
        (p, f) for p, f in stated if float(f.get("confidence") or 0) >= EXTRACTION_CONFIDENCE_THRESHOLD
    ]
    share = len(confident) / len(stated) if stated else 0.0
    print(f"  at or above {EXTRACTION_CONFIDENCE_THRESHOLD:.2f}       {len(confident)} ({share:.0%})")

    if not stated:
        print("FAIL: the pipeline committed a model with no stated facts")
        return 1
    if len(anchored) != len(stated):
        print(f"FAIL: {len(stated) - len(anchored)} stated facts have no anchor (D4)")
        return 1
    print("\nPASS: the production path committed anchored facts end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
