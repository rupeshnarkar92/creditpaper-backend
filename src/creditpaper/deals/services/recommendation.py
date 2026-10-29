"""The Section 9 recommendation (D8): written and signed by a person, never drafted by AI.

Ruled 2026-09-15 (docs/DECISIONS.md). A dedicated record, not Section 9 prose: generation
never writes here, and approval (`ledger.service.freeze_approval`) requires the latest
signed version. Every save is a new signed version in the append-only
`deal_recommendations` table (migration 0023), written through the tenant cursor so RLS
applies, with its activity event in the same transaction.
"""

from __future__ import annotations

from typing import Any

from creditpaper.db import tenant_cursor, tenant_uuid
from creditpaper.deals.services.activity import record_deal_event
from creditpaper.deals.services.activity_presenter import iso_utc
from creditpaper.ledger.types import Conflict, NotFound

MAX_BODY = 20000
SIGNED_ACTION = "paper.recommendation_signed"
UNKNOWN_AUTHOR = "Former member"
_LOCKED_STATES = {"APPROVED", "ISSUED", "ARCHIVED"}


class RecommendationEmpty(ValueError):
    """422 RECOMMENDATION_EMPTY: nothing but whitespace."""


class RecommendationTooLong(ValueError):
    """422 RECOMMENDATION_TOO_LONG: over MAX_BODY characters."""


def _author_names(tenant_id: str, author_ids: list[str]) -> dict[str, str]:
    from creditpaper.deals.store import activity_store

    return activity_store.actor_names(tenant_id, author_ids)


def _present(row: tuple[Any, ...], names: dict[str, str]) -> dict[str, Any]:
    rec_id, body, author_id, signed_at, ledger_revision = row[:5]
    return {
        "id": str(rec_id),
        "body": body,
        "author": {"id": str(author_id), "name": names.get(str(author_id)) or UNKNOWN_AUTHOR},
        "signed_at": iso_utc(signed_at),
        "ledger_revision": int(ledger_revision) if ledger_revision is not None else None,
    }


def get_recommendation(tenant_id: str, deal_id: str) -> dict[str, Any]:
    """`{current, history_count}`; `history_count` counts every signed version, current included."""
    tid = str(tenant_uuid(tenant_id))
    with tenant_cursor(tid) as cur:
        cur.execute("SELECT 1 FROM deals WHERE tenant_id = %s AND id = %s", (tid, deal_id))
        if not cur.fetchone():
            raise NotFound("Deal not found")
        cur.execute(
            """SELECT id, body, author_id, signed_at, ledger_revision, COUNT(*) OVER ()
            FROM deal_recommendations WHERE tenant_id = %s AND deal_id = %s
            ORDER BY signed_at DESC, id DESC LIMIT 1""",
            (tid, deal_id),
        )
        row = cur.fetchone()
    if not row:
        return {"current": None, "history_count": 0}
    names = _author_names(tid, [str(row[2])])
    return {"current": _present(row, names), "history_count": int(row[5])}


def sign_recommendation(tenant_id: str, deal_id: str, author_id: str, body: str | None) -> dict[str, Any]:
    """Record a new signed version by `author_id` (the signed-in user). Returns the new `current`.

    Raises RecommendationEmpty, RecommendationTooLong, NotFound, or Conflict when the paper is
    approved, issued or archived (reopen review first).
    """
    text = (body or "").strip()
    if not text:
        raise RecommendationEmpty("Write the recommendation before signing it.")
    if len(text) > MAX_BODY:
        raise RecommendationTooLong(f"The recommendation must be {MAX_BODY} characters or fewer.")
    tid = str(tenant_uuid(tenant_id))
    author = str(tenant_uuid(str(author_id)))
    # Looked up before the transaction so the deal lock is never held across a second connection.
    names = _author_names(tid, [author])
    author_name = names.get(author) or UNKNOWN_AUTHOR

    from creditpaper.ledger.service import lock_deal

    with tenant_cursor(tid) as cur:
        revision = lock_deal(cur, tid, deal_id)  # NotFound for another tenant's deal
        cur.execute("SELECT current_state FROM deals WHERE tenant_id = %s AND id = %s", (tid, deal_id))
        state = cur.fetchone()
        if state and state[0] in _LOCKED_STATES:
            raise Conflict(
                "Approved or archived papers keep their signed recommendation; reopen review first"
            )
        cur.execute(
            """INSERT INTO deal_recommendations (tenant_id, deal_id, body, author_id, ledger_revision)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, body, author_id, signed_at, ledger_revision""",
            (tid, deal_id, text, author, revision),
        )
        row = cur.fetchone()
        if row is None:  # INSERT ... RETURNING always yields a row; guards the type
            raise Conflict("The recommendation could not be recorded; try again")
        record_deal_event(
            cur,
            tenant_id=tid,
            deal_id=deal_id,
            actor_id=author,
            action=SIGNED_ACTION,
            entity_type="recommendation",
            entity_id=str(row[0]),
            metadata={"author": author_name, "length": len(text)},
        )
    return _present(row, {author: author_name})
