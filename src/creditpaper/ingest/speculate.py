"""Read every proposed document on a deal at once, into the hold.

`ledger/worker.py` hands a tenant ONE job per pass, deliberately, so that one
busy tenant cannot starve the others. That makes a job the wrong size for a
single document: the unit of SCHEDULING has to stay coarse while the unit of
WORK gets fine. So this is one job per deal that fans out across its documents
on a bounded pool — which is also the only place a provider rate limit and the
cost ledger's ceiling can actually be enforced.

Nothing here reaches the model. D3 as amended (2026-09-18) lets extraction START
on a PROPOSED slot precisely so the analyst is not the queue for a folder of
several hundred files; it does not let the result into the paper before a person
confirms the slot it was read under. `release.py` owns that transition.

What this replaces is worse than slow: every document used to be extracted
twice — once at upload on a filename guess, once at confirmation on the slot the
analyst chose. The saving here is mostly not doing the work twice, and only
secondarily doing it concurrently.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from creditpaper.concurrency import pool

log = logging.getLogger(__name__)

MAX_WORKERS = 6

# A slot the analyst has already decided is not speculative: its facts are in
# the model, or on their way there through the ordinary confirmed path.
_DECIDED = ("confirmed", "processing", "failed")


class SpeculationReport(BaseModel):
    documents: int = 0
    held: int = 0
    failed: int = 0
    skipped: int = 0
    # Read speculatively, but confirmed before the read finished, so the facts
    # went straight into the model rather than waiting in a hold nobody owns.
    released: int = 0


class _Pending(BaseModel):
    document_id: str
    version_id: str
    content_hash: str
    slot_key: str


def _pending(tenant_id: str, deal_id: str) -> tuple[list[_Pending], int]:
    """Proposed documents worth reading, and how many were skipped.

    Three exclusions, each for its own reason:
      * a DECIDED slot's facts are already in the model, or already queued;
      * a slot whose taxonomy key maps to no extractor is the disclosure list
        Flow E describes, not a failure ("the board is not the extractor
        registry");
      * a document already held was read on an earlier pass, and reading it
        again would spend twice for the same answer.
    """
    from creditpaper.db import tenant_cursor, tenant_uuid
    from creditpaper.deals.services.slot_matcher import routing_keys_for
    from creditpaper.deals.store import slot_store
    from creditpaper.ingest import intake_store

    slots = {s["id"]: s for s in slot_store.list_slots(tenant_id, deal_id)}
    assigned = [
        f
        for f in slot_store.list_slot_files(tenant_id, deal_id)
        if f["role"] == "assigned" and f.get("version_id")
    ]
    if not assigned:
        return [], 0

    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT id, content_sha256 FROM deal_document_versions WHERE tenant_id = %s AND id = ANY(%s)",
            (tenant_uuid(tenant_id), sorted({str(f["version_id"]) for f in assigned})),
        )
        hashes = {str(r[0]): str(r[1]) for r in cur.fetchall() if r[1]}

    out: list[_Pending] = []
    skipped = 0
    for f in assigned:
        slot = slots.get(str(f["slot_id"]))
        if slot is None or slot["status"] in _DECIDED:
            continue
        content_hash = hashes.get(str(f["version_id"]))
        if content_hash is None:
            skipped += 1
            continue
        if not routing_keys_for(slot["slot_key"]):
            # No extractor reads this slot. Flow E: the difference between the
            # board and the registry is the disclosure list, not an error.
            skipped += 1
            continue
        if intake_store.get_held_facts(tenant_id, deal_id, content_hash) is not None:
            continue
        out.append(
            _Pending(
                document_id=str(f["document_id"]),
                version_id=str(f["version_id"]),
                content_hash=content_hash,
                slot_key=str(slot["slot_key"]),
            )
        )
    return out, skipped


def speculate_deal(
    tenant_id: str,
    deal_id: str,
    *,
    registry: dict[str, type] | None = None,
    max_workers: int = MAX_WORKERS,
) -> SpeculationReport:
    from creditpaper.ingest.document import run_document

    pending, skipped = _pending(tenant_id, deal_id)
    report = SpeculationReport(documents=len(pending), skipped=skipped)
    if not pending:
        return report

    def _one(item: _Pending) -> bool:
        # Per-document failure, never per deal: one odd document must not cost
        # the folder its whole extraction, which has happened here (2026-08-21).
        try:
            run_document(
                tenant_id,
                deal_id,
                version_id=item.version_id,
                content_hash=item.content_hash,
                proposed_slot_key=item.slot_key,
                document_id=item.document_id,
                registry=registry,
            )
            return True
        except Exception:  # noqa: BLE001 - the failure is the finding
            log.exception("speculative read failed for one document")
            return False

    # pool(), not a bare executor: these call the model, and cost attribution
    # travels in a ContextVar that a raw ThreadPoolExecutor drops.
    with pool(min(max_workers, len(pending)), thread_name_prefix="speculate") as executor:
        outcomes = list(executor.map(_one, pending))

    report.held = sum(1 for ok in outcomes if ok)
    report.failed = sum(1 for ok in outcomes if not ok)
    report.released = _release_confirmed_while_reading(
        tenant_id, deal_id, [p for p, ok in zip(pending, outcomes, strict=True) if ok]
    )
    return report


def _release_confirmed_while_reading(tenant_id: str, deal_id: str, read: list[_Pending]) -> int:
    """Release a read whose slot the analyst confirmed while it was in flight.

    The race this closes: confirmation looks for a hold, finds none because the
    read has not finished, and falls back to extracting. The read then lands a
    hold that nothing will ever release — an orphan, and the spend wasted.

    Checked here rather than coordinated with the confirmation, because the read
    finishing is the later of the two events and therefore the one that can see
    both. A confirmation that already extracted still wins on correctness; this
    only stops the hold being abandoned.
    """
    from creditpaper.deals.store import slot_store
    from creditpaper.ingest import release

    if not read:
        return 0

    # Where each document is assigned NOW, and whether that slot is decided.
    # Not "is the slot this was read under decided": the analyst may have moved
    # the document somewhere else and confirmed a different file into the
    # original slot, and releasing on the slot key alone would then cite these
    # facts to a slot the document no longer sits in.
    slots = {s["id"]: s for s in slot_store.list_slots(tenant_id, deal_id)}
    assigned_to: dict[str, str] = {}
    for f in slot_store.list_slot_files(tenant_id, deal_id):
        slot = slots.get(str(f["slot_id"]))
        if f["role"] == "assigned" and slot is not None and slot["status"] in _DECIDED:
            assigned_to[str(f["document_id"])] = str(slot["slot_key"])

    released = 0
    for item in read:
        confirmed_slot = assigned_to.get(item.document_id)
        if confirmed_slot is None:
            continue
        try:
            # The confirmed slot, not the proposed one. When they differ,
            # release_or_discard discards this read and queues the right
            # extractor — which is exactly the correct outcome.
            outcome = release.release_or_discard(tenant_id, deal_id, item.content_hash, confirmed_slot, "")
        except Exception:  # noqa: BLE001 - the hold survives for a later attempt
            log.exception("could not release a read confirmed while it was in flight")
            continue
        if outcome.outcome is release.ReleaseOutcome.RELEASED:
            released += 1
    return released


def run_speculation(tenant_id: str, deal_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One durable speculation job for a deal."""
    report = speculate_deal(tenant_id, deal_id)
    # Counts only. No filename, slot key or document text enters the job log.
    return {"status": "complete", **report.model_dump()}
