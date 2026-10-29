"""Deal activity: the one way a deal event is recorded, and the paged read of them.

Recording (contract 2026-09-15, cloned from Ceralinq `log_event`): a row in the append-only
`deal_audit_events` holds structured data only, an action slug plus metadata. Human text is
built on read by `activity_presenter`. Metadata carries names, ids, counts and short error
codes; never secrets or document content.

  - `record_deal_event(cur, ...)` inserts inside the caller's transaction and never commits,
    so a business write and its event land together.
  - `record_deal_event_best_effort(...)` is for background work (extraction, generation): it
    opens its own short transaction and a failure is logged, never raised into the job.

Reading: `list_activity` pages newest first on (created_at, id), filters by category, and
reports total and per-category counts that ignore the filter. A deal with no stored
`deal.created` row (created before this build) gets one synthesised from `deals.created_at`
at read time; it is never written.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import UTC, datetime
from typing import Any

from creditpaper.db import tenant_cursor

log = logging.getLogger(__name__)

INSERT_SQL = """
    INSERT INTO deal_audit_events (
        tenant_id, deal_id, actor_id, action, entity_type, entity_id,
        event_metadata, correlation_id, created_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
"""

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
SYNTHETIC_ID_PREFIX = "synthetic-deal-created-"


def record_deal_event(
    cur: Any,
    *,
    tenant_id: Any,
    deal_id: str,
    actor_id: Any,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    created_at: datetime | None = None,
) -> None:
    """Append one event using the caller's cursor. Never commits.

    `tenant_id` and `actor_id` are passed to the driver as given (str or UUID), so call sites
    keep the parameter values they always wrote.
    """
    cur.execute(
        INSERT_SQL,
        (
            tenant_id,
            deal_id,
            actor_id,
            action,
            entity_type,
            entity_id,
            json.dumps(metadata or {}, default=str),
            correlation_id,
            created_at or datetime.now(UTC),
        ),
    )


def record_deal_event_best_effort(
    *,
    tenant_id: Any,
    deal_id: str,
    actor_id: Any,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> bool:
    """Record an event in its own short transaction. Returns False (and logs) on any failure."""
    try:
        with tenant_cursor(str(tenant_id)) as cur:
            with cur.connection.transaction():
                record_deal_event(
                    cur,
                    tenant_id=tenant_id,
                    deal_id=deal_id,
                    actor_id=actor_id,
                    action=action,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    metadata=metadata,
                    correlation_id=correlation_id,
                )
        return True
    except Exception as exc:  # noqa: BLE001 — activity recording never breaks the job
        log.warning("activity event %s for deal %s not recorded: %s", action, deal_id, type(exc).__name__)
        return False


# --- read ------------------------------------------------------------------------------


class DealNotFound(Exception):
    """404 DEAL_NOT_FOUND. Another tenant's deal is indistinguishable from none."""


class InvalidActivityQuery(Exception):
    """422 with the error envelope: bad limit, category or cursor."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(message)


def encode_cursor(created_at: datetime, event_id: str) -> str:
    raw = json.dumps({"t": created_at.isoformat(), "i": str(event_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        created_at = datetime.fromisoformat(str(data["t"]))
        event_id = str(data["i"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError) as exc:
        raise InvalidActivityQuery("cursor", "This cursor is not valid.") from exc
    if created_at.tzinfo is None or not event_id:
        raise InvalidActivityQuery("cursor", "This cursor is not valid.")
    return created_at, event_id


def _validate(category: str | None, limit: int) -> None:
    from creditpaper.deals.services.activity_presenter import CATEGORIES

    if category is not None and category not in CATEGORIES:
        raise InvalidActivityQuery("category", f"category must be one of: {', '.join(CATEGORIES)}.")
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidActivityQuery("limit", f"limit must be between 1 and {MAX_LIMIT}.")


def synthetic_deal_created(deal: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"{SYNTHETIC_ID_PREFIX}{deal['id']}",
        "actor_id": None,
        "action": "deal.created",
        "entity_type": "deal",
        "entity_id": deal["id"],
        "metadata": {"name": deal.get("name"), "synthetic": True},
        "created_at": deal.get("created_at"),
    }


def page_actor_names(tenant_id: str, rows: list[dict[str, Any]]) -> dict[str, str]:
    """One lookup for the whole page. A failed lookup shows the platform name, never an error."""
    from creditpaper.deals.store import activity_store

    ids = sorted({str(r["actor_id"]) for r in rows if r.get("actor_id")})
    if not ids:
        return {}
    try:
        return activity_store.actor_names(tenant_id, ids)
    except Exception as exc:  # noqa: BLE001
        log.warning("activity actor names unavailable: %s", type(exc).__name__)
        return {}


def present_rows(tenant_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from creditpaper.deals.services import activity_presenter as presenter

    names = page_actor_names(tenant_id, rows)
    return [presenter.present(r, names) for r in rows]


def list_activity(
    tenant_id: str,
    deal_id: str,
    *,
    category: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor_token: str | None = None,
) -> dict[str, Any]:
    """The Activity page payload. Raises DealNotFound or InvalidActivityQuery."""
    from creditpaper.db import tenant_cursor as _cursor
    from creditpaper.db import tenant_uuid as _tid
    from creditpaper.deals.services import activity_presenter as presenter

    _validate(category, limit)
    before = decode_cursor(cursor_token) if cursor_token else None
    tid = _tid(tenant_id)

    with _cursor(str(tid)) as cur:
        cur.execute(
            "SELECT id, name, created_at FROM deals WHERE id = %s AND tenant_id = %s",
            (deal_id, tid),
        )
        deal_row = cur.fetchone()
        if deal_row is None:
            raise DealNotFound(deal_id)
        deal = {"id": str(deal_row[0]), "name": deal_row[1], "created_at": deal_row[2]}

        cur.execute(
            "SELECT action, COUNT(*) FROM deal_audit_events "
            "WHERE tenant_id = %s AND deal_id = %s GROUP BY action",
            (tid, deal_id),
        )
        action_counts = {str(r[0]): int(r[1]) for r in cur.fetchall()}
        needs_synthetic = action_counts.get("deal.created", 0) == 0
        counts = dict.fromkeys(presenter.CATEGORIES, 0)
        for action, n in action_counts.items():
            counts[presenter.category_for(action)] += n
        if needs_synthetic:
            counts["deal"] += 1
        total = sum(counts.values())

        actions: list[str] | None = None
        if category is not None:
            actions = sorted(a for a in action_counts if presenter.category_for(a) == category)

        rows: list[dict[str, Any]] = []
        if actions != []:
            clauses = ["tenant_id = %s", "deal_id = %s"]
            params: list[Any] = [tid, deal_id]
            if actions is not None:
                placeholders = ", ".join(["%s"] * len(actions))
                clauses.append(f"action IN (SELECT unnest(ARRAY[{placeholders}]))")
                params.extend(actions)
            if before is not None:
                clauses.append("(created_at, id) < (%s, %s::uuid)")
                params.extend([before[0], before[1]])
            params.append(limit + 1)
            cur.execute(
                "SELECT id, actor_id, action, entity_type, entity_id, event_metadata, created_at "
                "FROM deal_audit_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, id DESC LIMIT %s",
                params,
            )
            raw = cur.fetchall()
            rows = [
                {
                    "id": str(r[0]),
                    "actor_id": str(r[1]) if r[1] else None,
                    "action": r[2],
                    "entity_type": r[3],
                    "entity_id": r[4],
                    "metadata": r[5] if isinstance(r[5], dict) else {},
                    "created_at": r[6],
                }
                for r in raw
            ]

        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = encode_cursor(rows[-1]["created_at"], rows[-1]["id"])
        elif needs_synthetic and category in (None, "deal"):
            if len(rows) < limit:
                rows.append(synthetic_deal_created(deal))
            else:
                next_cursor = encode_cursor(rows[-1]["created_at"], rows[-1]["id"])

        ids = sorted({str(r["actor_id"]) for r in rows if r.get("actor_id")})
        names: dict[str, str] = {}
        if ids:
            placeholders = ", ".join(["%s"] * len(ids))
            try:
                cur.execute(
                    "SELECT id, first_name, last_name, email FROM users "
                    f"WHERE id IN (SELECT unnest(ARRAY[{placeholders}])) AND domain_id = %s",
                    [*ids, str(tid)],
                )
                for user_id, first, last, email in cur.fetchall():
                    name = " ".join(p for p in ((first or "").strip(), (last or "").strip()) if p)
                    if not name and email:
                        name = str(email).split("@", 1)[0]
                    if name:
                        names[str(user_id)] = name
            except Exception as exc:  # noqa: BLE001 — a failed lookup shows the platform name
                log.warning("activity actor names unavailable: %s", type(exc).__name__)

    events = [presenter.present(r, names) for r in rows]
    return {
        "deal_id": deal_id,
        "total": total,
        "counts": counts,
        "events": events,
        "next_cursor": next_cursor,
    }


def activity_from_raw(deal_id: str, raw: dict[str, Any], *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Build first Activity page from get_workspace_page() data without another DB read."""
    from creditpaper.deals.services import activity_presenter as presenter

    deal = raw.get("deal") or {"id": deal_id}
    action_counts = {str(k): int(v) for k, v in (raw.get("action_counts") or {}).items()}
    needs_synthetic = action_counts.get("deal.created", 0) == 0
    counts = dict.fromkeys(presenter.CATEGORIES, 0)
    for action, count in action_counts.items():
        counts[presenter.category_for(action)] += count
    if needs_synthetic:
        counts["deal"] += 1
    rows = list(raw.get("events") or [])
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        created_at = rows[-1]["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        next_cursor = encode_cursor(created_at, rows[-1]["id"])
    elif needs_synthetic and len(rows) < limit:
        rows.append(synthetic_deal_created(deal))
    return {
        "deal_id": deal_id,
        "total": sum(counts.values()),
        "counts": counts,
        "events": [presenter.present(row, raw.get("names") or {}) for row in rows],
        "next_cursor": next_cursor,
    }


def list_tenant_activity(
    tenant_id: str,
    *,
    category: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor_token: str | None = None,
) -> dict[str, Any]:
    """Home: the tenant's activity across its deals, newest first. Each event is the per-deal
    Activity shape plus `deal: {id, name}`. No synthetic `deal.created` events. Raises
    InvalidActivityQuery."""
    from creditpaper.deals.services import activity_presenter as presenter
    from creditpaper.deals.store import activity_store

    _validate(category, limit)
    before = decode_cursor(cursor_token) if cursor_token else None
    raw = activity_store.call_tenant_activity(tenant_id, category=category, limit=limit, before=before)
    counts = dict.fromkeys(presenter.CATEGORIES, 0)
    for action, n in raw["counts"].items():
        counts[presenter.category_for(action)] += n
    total = sum(counts.values())
    rows = raw["events"]
    names = raw["names"]
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(datetime.fromisoformat(rows[-1]["created_at"]), rows[-1]["id"])

    presented = [presenter.present(r, names) for r in rows]
    events = [{**event, "deal": row["deal"]} for event, row in zip(presented, rows, strict=True)]
    return {"total": total, "counts": counts, "events": events, "next_cursor": next_cursor}
