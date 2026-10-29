"""A confirmed proposal becomes a filed document and a queued extraction.

Intake's own tables record what was proposed and what the analyst said. They are
not the filing system: the product files documents in `document_slots` and
`document_slot_files`, and the board, the fact sheet and the paper all read from
there. A confirmation that stopped at a label would leave the analyst looking at
an empty board having just filed a folder.

Extraction is queued through `enqueue_slot_extraction`, which already carries the
D3 rule that the confirmed slot - not the filename classifier - picks the
extractor.
"""

from __future__ import annotations

from pydantic import BaseModel

from creditpaper.deals.services.slot_board import ExtractionJob, enqueue_slot_extraction
from creditpaper.deals.store import slot_store


class FiledDocument(BaseModel):
    slot_key: str
    slot_id: str
    job_id: str | None = None


def _extractor_for(slot_key: str) -> str | None:
    for row in slot_store.slot_types():
        if row["key"] == slot_key:
            extractor = row.get("extractor")
            return str(extractor) if extractor else None
    raise ValueError(f"unknown slot {slot_key!r}")


def file_confirmed_document(
    tenant_id: str,
    deal_id: str,
    *,
    slot_key: str,
    document_id: str,
    version_id: str,
    confidence: float,
    match_reason: str,
    confirmed_by: str,
    enqueue: bool = True,
) -> FiledDocument:
    extractor = _extractor_for(slot_key)

    slot_store.ensure_slots(tenant_id, deal_id, [slot_key])
    slot_id = next(
        (s["id"] for s in slot_store.list_slots(tenant_id, deal_id) if s["slot_key"] == slot_key),
        None,
    )
    if slot_id is None:  # pragma: no cover - ensure_slots just created it
        raise ValueError(f"slot {slot_key!r} could not be created for this deal")

    slot_store.write_changes(
        tenant_id=tenant_id,
        deal_id=deal_id,
        # The board's audit trail names who confirmed, not the service.
        actor_id=confirmed_by,
        slot_updates=[
            {
                "id": slot_id,
                "status": "confirmed",
                "confidence": confidence,
                "match_reason": match_reason,
            }
        ],
        add_files=[
            {
                "slot_id": slot_id,
                "document_id": document_id,
                "version_id": version_id,
                # The board knows two roles: a confirmation is an assignment,
                # not a candidate the machine merely suggested.
                "role": "assigned",
                "confidence": confidence,
                "match_reason": match_reason,
            }
        ],
        audit=[
            {
                "action": "intake.confirmed",
                "entity_type": "document_slot",
                "entity_id": str(slot_id),
                "metadata": {"slot_key": slot_key, "confidence": confidence},
            }
        ],
    )

    job_id: str | None = None
    if enqueue and extractor:
        job_id = enqueue_slot_extraction(
            tenant_id,
            deal_id,
            ExtractionJob(document_id=document_id, version_id=version_id, extractor=extractor),
        )
    return FiledDocument(slot_key=slot_key, slot_id=str(slot_id), job_id=job_id)
