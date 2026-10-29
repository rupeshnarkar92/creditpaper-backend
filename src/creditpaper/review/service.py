"""Fact review queue — routes low-confidence extractions to human review.

After extraction, facts with confidence below EXTRACTION_CONFIDENCE_THRESHOLD
are placed in the review queue. Analysts approve, correct, or reject them.
Approved/corrected facts are written to the DealModel with HUMAN_ENTRY source.
"""

from __future__ import annotations

from typing import Any

from creditpaper.db import tenant_cursor, tenant_uuid


def route_to_review(
    tenant_id: str,
    deal_id: str,
    field_path: str,
    document_id: str,
    extracted_value: Any,
    confidence: float,
    page: int | None = None,
) -> str | None:
    """Add a low-confidence fact to the review queue. Returns the queue entry ID."""
    from creditpaper.config import EXTRACTION_CONFIDENCE_THRESHOLD

    if confidence >= EXTRACTION_CONFIDENCE_THRESHOLD:
        return None  # above threshold, no review needed

    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.add_fact_review_item(%s,%s,%s,%s,%s,%s,%s)",
            (
                tenant_uuid(tenant_id),
                deal_id,
                field_path,
                document_id,
                page,
                str(extracted_value) if extracted_value is not None else None,
                confidence,
            ),
        )
        return str(cur.fetchone()[0])


def get_pending_reviews(tenant_id: str, deal_id: str) -> list[dict]:
    """Get all pending review items for a deal."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.pending_fact_reviews(%s,%s)", (tenant_uuid(tenant_id), deal_id))
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def _record_fact_event(
    cur: Any,
    *,
    tenant_id: Any,
    deal_id: str,
    reviewer_id: str,
    action: str,
    queue_id: str,
    path: str,
    **extra: Any,
) -> None:
    """Activity event in the review transaction: the path and its fact-sheet label, never the value."""
    from creditpaper.deals.services.activity import record_deal_event
    from creditpaper.deals.services.fact_sheet import field_label

    try:
        label = field_label(path)
    except Exception:  # noqa: BLE001 — a label is presentation; the path is the record
        label = path
    record_deal_event(
        cur,
        tenant_id=tenant_id,
        deal_id=deal_id,
        actor_id=reviewer_id,
        action=action,
        entity_type="fact_review",
        entity_id=str(queue_id),
        metadata={"path": path, "label": label, **extra},
    )


def _fact_event_hook(tenant_id: Any, deal_id: str, reviewer_id: str, queue_id: str, action: str):
    """The `on_decided` callback: the activity event is written in the decision's transaction."""

    def hook(cur: Any, status: str, path: str) -> None:
        extra: dict[str, Any] = {"corrected": status == "corrected"} if action == "fact.approved" else {}
        _record_fact_event(
            cur,
            tenant_id=tenant_id,
            deal_id=deal_id,
            reviewer_id=reviewer_id,
            action=action,
            queue_id=queue_id,
            path=path,
            **extra,
        )

    return hook


def approve_fact(
    queue_id: str,
    reviewer_id: str,
    reviewed_value: str | None = None,
    notes: str | None = None,
    *,
    tenant_id: str,
    deal_id: str,
    expected_revision: int,
) -> dict:
    """Approve or correct a fact in the review queue, through the fact ledger.

    If reviewed_value is provided, status = 'corrected'.
    If reviewed_value is None, status = 'approved' (extracted value was correct).

    A `fact.approved` Activity event is written in the same transaction.
    """
    from creditpaper.ledger.service import review_queue_item

    return review_queue_item(
        tenant_id,
        deal_id,
        queue_id,
        reviewer_id,
        action="accept",
        reason=notes or "Reviewed by analyst",
        expected_revision=expected_revision,
        corrected_value=reviewed_value,
        on_decided=_fact_event_hook(tenant_id, deal_id, reviewer_id, queue_id, "fact.approved"),
    )


def reject_fact(
    queue_id: str,
    reviewer_id: str,
    notes: str | None = None,
    *,
    tenant_id: str,
    deal_id: str,
    expected_revision: int,
) -> dict:
    """Reject a fact — it was incorrectly extracted and should not be used.

    A `fact.rejected` Activity event is written in the same transaction.
    """
    from creditpaper.ledger.service import review_queue_item

    return review_queue_item(
        tenant_id,
        deal_id,
        queue_id,
        reviewer_id,
        action="reject",
        reason=notes or "Rejected by analyst",
        expected_revision=expected_revision,
        on_decided=_fact_event_hook(tenant_id, deal_id, reviewer_id, queue_id, "fact.rejected"),
    )


def get_review_stats(tenant_id: str, deal_id: str) -> dict:
    """Get review queue statistics for a deal."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.fact_review_stats(%s,%s)", (tenant_uuid(tenant_id), deal_id))
        stats = {}
        for row in cur.fetchall():
            stats[row[0]] = {"count": row[1], "avg_confidence": row[2]}
        return stats
