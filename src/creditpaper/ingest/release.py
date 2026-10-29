"""Confirmation is what releases speculative facts into the deal.

D3 as amended (2026-09-18): extraction may start on the *proposed* slot as soon
as a document is parsed, so the analyst is not the queue for a folder of several
hundred documents. What that amendment does not change is which facts reach the
paper: they are held until a person confirms the slot they were read under.

Three outcomes, and the middle one is the point:

  * The confirmed slot is the proposed slot - the facts were read under the
    right assumption, so they go into the model through the ledger's own write
    path, which locks the deal and updates the projection atomically.
  * A different slot is confirmed - the facts were read by the wrong extractor
    and are discarded, not reinterpreted. The correct extractor is queued
    instead. The discarded run stays in the job record for cost and audit, as
    the amendment requires, but never in the model.
  * Nothing is held - a confirmation with no speculative run behind it, which
    is the ordinary case for a document confirmed before extraction finished.

Releasing twice releases once: the hold is dropped as part of the release, so a
repeated confirmation finds nothing held and says so.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel

from creditpaper.ingest import intake_store
from creditpaper.ingest.commit import VerifiedFact, to_output
from creditpaper.ingest.gaps import Coverage


class ReleaseOutcome(str, Enum):
    RELEASED = "released"
    DISCARDED_AND_REQUEUED = "discarded_and_requeued"
    NOTHING_HELD = "nothing_held"


class ReleaseResult(BaseModel):
    outcome: ReleaseOutcome
    released: int = 0
    discarded: int = 0
    requeued_job_id: str | None = None


def hold(
    tenant_id: str,
    deal_id: str,
    content_hash: str,
    proposed_slot_key: str,
    facts: list[VerifiedFact],
    coverage: Coverage,
) -> None:
    """Park a speculative extraction against the slot it was read under."""
    intake_store.put_held_facts(
        tenant_id,
        deal_id,
        content_hash,
        proposed_slot_key=proposed_slot_key,
        facts=[f.model_dump(mode="json") for f in facts],
        coverage=coverage.model_dump(mode="json"),
    )


def release_or_discard(
    tenant_id: str,
    deal_id: str,
    content_hash: str,
    confirmed_slot_key: str,
    confirmed_by: str,
) -> ReleaseResult:
    held = intake_store.get_held_facts(tenant_id, deal_id, content_hash)
    if held is None:
        return ReleaseResult(outcome=ReleaseOutcome.NOTHING_HELD)

    proposed_slot_key, raw_facts, raw_coverage = held

    if proposed_slot_key != confirmed_slot_key:
        # Read by the wrong extractor: the values are not reinterpreted, they
        # are dropped and the right extractor is queued.
        intake_store.drop_held_facts(tenant_id, deal_id, content_hash)
        job_id = _requeue(tenant_id, deal_id, content_hash, confirmed_slot_key, confirmed_by)
        return ReleaseResult(
            outcome=ReleaseOutcome.DISCARDED_AND_REQUEUED,
            discarded=len(raw_facts),
            requeued_job_id=job_id,
        )

    facts = [VerifiedFact.model_validate(f) for f in raw_facts]
    coverage = Coverage.model_validate(raw_coverage or {"requested": (), "read": (), "failed": ()})
    output = to_output(facts, coverage)

    from creditpaper.ledger.service import commit_outputs

    # The ledger's own write path: it takes the deal lock, fences against a
    # stale worker, and updates the projection and snapshot atomically.
    commit_outputs(tenant_id, deal_id, [output])
    intake_store.drop_held_facts(tenant_id, deal_id, content_hash)
    return ReleaseResult(outcome=ReleaseOutcome.RELEASED, released=len(facts))


def _requeue(tenant_id: str, deal_id: str, content_hash: str, slot_key: str, confirmed_by: str) -> str | None:
    """Queue the confirmed slot's extractor for this document, if it is here."""
    from creditpaper.ingest.filing import file_confirmed_document
    from creditpaper.ingest.jobs import version_for_hash

    target: dict[str, Any] | None = version_for_hash(tenant_id, deal_id, content_hash)
    if target is None:
        return None
    filed = file_confirmed_document(
        tenant_id,
        deal_id,
        slot_key=slot_key,
        document_id=target["document_id"],
        version_id=target["version_id"],
        confidence=1.0,
        match_reason="confirmed by analyst; speculative extraction discarded",
        confirmed_by=confirmed_by,
    )
    return filed.job_id
