"""Proposing a folder, as work that can be handed to the queue.

The loop lives here rather than in the route because D3 as amended (2026-09-18)
requires ingestion work to be durable: enqueued server-side, surviving a browser
refresh, a closed window and a worker restart. A folder of several hundred
documents calls the model per document, which is not something an analyst should
hold a connection open for.

One document's failure is that document's failure. The batch records it and
keeps going, which is the same rule the page-level fan-out follows.

Nothing here trusts the caller for what a document says: the filename, the
content hash and the page text all come from the version row and the extraction
the pipeline wrote. The folder path is a hint that feeds one signal and can
reach neither the cache nor the labels.
"""

from __future__ import annotations

import logging
from typing import Any

from creditpaper.db import tenant_cursor
from creditpaper.ingest import intake_store, parse_cache
from creditpaper.ingest.intake_store import labelled_corpus
from creditpaper.ingest.page_text import first_pages, pages_for_version
from creditpaper.ingest.proposal import propose_slot
from creditpaper.ingest.refusal import check_refusal

log = logging.getLogger(__name__)


def version_facts(tenant_id: str, version_id: str) -> dict[str, str] | None:
    """What the server knows about an uploaded version, or None if it is not ours."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT document_id, original_filename, content_sha256 "
            "FROM deal_document_versions WHERE tenant_id = %s AND id = %s",
            (tenant_id, version_id),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "document_id": str(row[0]),
        "filename": row[1] or "",
        "content_hash": row[2] or "",
    }


def run_propose(tenant_id: str, deal_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    documents = payload.get("documents") or []
    party_names = payload.get("party_names")
    # Once for the batch: the corpus is identical for every document in it, and
    # shingling it is the expensive part.
    corpus = labelled_corpus(tenant_id)
    proposed = refused = not_parsed = failed = 0
    rows: list[dict[str, Any]] = []

    for document in documents:
        version_id = str(document.get("version_id") or "")
        path = str(document.get("path") or "")
        try:
            facts = version_facts(tenant_id, version_id)
            parsed = pages_for_version(tenant_id, version_id) if facts else None
            if facts is None or parsed is None:
                # Not an empty document: the pipeline has not finished with it,
                # or the version is not this tenant's. Say so rather than
                # proposing from nothing.
                not_parsed += 1
                rows.append(
                    {
                        "outcome": "not_parsed",
                        "slot_key": None,
                        "version_id": version_id,
                        "filename": facts["filename"] if facts else "",
                    }
                )
                continue

            opening = first_pages(parsed.pages)
            first_page = opening.get(min(opening), "") if opening else ""
            refusal = check_refusal(facts["filename"], first_page)
            if refusal.refused:
                refused += 1
                intake_store.record_proposal(
                    tenant_id,
                    deal_id,
                    content_hash=facts["content_hash"],
                    filename=facts["filename"],
                    outcome="refused",
                    slot_key=None,
                    confidence=0.0,
                    evidence=[
                        {
                            "signal": "refusal",
                            "slot_key": None,
                            "score": 1.0,
                            "detail": refusal.reason or "",
                        }
                    ],
                    needs_party_context=False,
                )
                rows.append(
                    {
                        "outcome": "refused",
                        "slot_key": None,
                        "content_hash": facts["content_hash"],
                        "version_id": version_id,
                        "filename": facts["filename"],
                        "reason": refusal.reason,
                        "refused_class": refusal.refused_class,
                    }
                )
                continue

            parse_cache.put(
                tenant_id,
                facts["content_hash"],
                version_id=version_id,
                route=parsed.route,
                page_count=parsed.page_count,
            )
            proposal = propose_slot(
                tenant_id,
                deal_id,
                content_hash=facts["content_hash"],
                filename=facts["filename"],
                path=path,
                first_pages=opening,
                party_names=party_names,
                labelled=corpus,
            )
            intake_store.record_proposal(
                tenant_id,
                deal_id,
                content_hash=facts["content_hash"],
                filename=facts["filename"],
                outcome=proposal.outcome.value,
                slot_key=proposal.slot_key,
                confidence=proposal.confidence,
                evidence=[e.model_dump() for e in proposal.evidence],
                needs_party_context=proposal.needs_party_context,
            )
            proposed += 1
            rows.append(
                {
                    "outcome": proposal.outcome.value,
                    "slot_key": proposal.slot_key,
                    "content_hash": facts["content_hash"],
                    "version_id": version_id,
                    "document_id": facts["document_id"],
                    "filename": facts["filename"],
                    "confidence": proposal.confidence,
                    "needs_party_context": proposal.needs_party_context,
                    "evidence": [e.model_dump() for e in proposal.evidence],
                }
            )
        except Exception as exc:  # one document's failure is not the folder's
            failed += 1
            log.warning("intake proposal failed for one document: %s", type(exc).__name__)

    return {
        "status": "complete",
        "proposed": proposed,
        "refused": refused,
        "not_parsed": not_parsed,
        "failed": failed,
        "rows": rows,
    }


def enqueue_propose(
    tenant_id: str,
    deal_id: str,
    documents: list[dict[str, str]],
    party_names: dict[str, str] | None,
) -> str:
    """Hand a folder to the durable queue and return the job to watch."""
    from creditpaper.ledger import jobs

    job_id = jobs.enqueue(
        tenant_id,
        deal_id,
        "ingest_propose",
        f"intake:{len(documents)}",
        {"documents": documents, "party_names": party_names},
    )
    jobs.dispatch(tenant_id, job_id)
    return job_id


def version_for_hash(tenant_id: str, deal_id: str, content_hash: str) -> dict[str, str] | None:
    """The newest version of this deal carrying those bytes, or None.

    A confirmation names a content hash; filing needs the document and version
    it belongs to on THIS deal.
    """
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT id, document_id FROM deal_document_versions "
            "WHERE tenant_id = %s AND deal_id = %s AND content_sha256 = %s "
            "ORDER BY version_number DESC LIMIT 1",
            (tenant_id, deal_id, content_hash),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"version_id": str(row[0]), "document_id": str(row[1])}
