"""The contradiction register: every same-concept disagreement on a deal (spec 3a).

`commit_outputs` classifies each new assertion against the other live assertions of
the same concept (`ingest/compare.py`) and records every difference that is not an
agreement here. None is swallowed:

- a CONTRADICTION is recorded `open`. On a material concept it keeps the concept out
  of the projection (`service._project`) and fails check #18 until an analyst
  resolves it (D5: stop and ask);
- a BASIS_DIFFERENCE, ROUNDING, SUPERSEDED or TEMPORAL difference is recorded
  `noted`: informational, blocking nothing.

Rules never hide a disagreement: a resolution updates the row's status and records
the value, the analyst, the time and a reason; the row itself is never deleted (the
runtime role has no DELETE). A resolution is also a label (`ledger/labels.py`), so it
feeds the feedback loop (spec 3c).

`record` and `settle` take the caller's cursor, so they share the ledger write's
transaction and its `lock_deal` discipline.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from creditpaper.db import tenant_cursor
from creditpaper.ingest.compare import DiffClass
from creditpaper.ledger.types import Conflict, NotFound

if TYPE_CHECKING:
    from creditpaper.model.deal import DealModel

log = logging.getLogger(__name__)

__all__ = [
    "Contradiction",
    "list_contradictions",
    "open_material_paths",
    "record",
    "resolve",
    "settle",
]

_COLUMNS = (
    "id",
    "tenant_id",
    "deal_id",
    "concept_path",
    "assertion_ids",
    "diff_class",
    "material",
    "status",
    "resolved_value",
    "resolved_assertion_id",
    "resolved_by",
    "reason",
    "resolved_at",
    "registry_version",
    "created_at",
)


@dataclass(frozen=True)
class Contradiction:
    id: str
    tenant_id: str
    deal_id: str
    concept_path: str
    assertion_ids: tuple[str, ...]
    diff_class: str
    material: bool
    status: str
    resolved_value: Any
    resolved_assertion_id: str | None
    resolved_by: str | None
    reason: str | None
    resolved_at: datetime | None
    registry_version: str
    created_at: datetime

    @property
    def blocking(self) -> bool:
        """An open contradiction on a material concept: the concept stays out of the paper."""
        return self.status == "open" and self.material and self.diff_class == DiffClass.CONTRADICTION.value


def _row(row: tuple[Any, ...]) -> Contradiction:
    data = dict(zip(_COLUMNS, row, strict=True))
    for key in ("id", "tenant_id", "resolved_assertion_id", "resolved_by"):
        if data[key] is not None:
            data[key] = str(data[key])
    data["assertion_ids"] = tuple(str(a) for a in data["assertion_ids"])
    return Contradiction(**data)


def record(
    cur: Any,
    *,
    tenant_id: str,
    deal_id: str,
    concept_path: str,
    assertion_ids: list[str],
    diff_class: DiffClass,
    material: bool,
    registry_version: str,
    now: datetime,
) -> str:
    """Record one difference on the caller's cursor. Returns the register row id.

    One unresolved row per (concept, class): a further assertion joins the open (or
    noted) row instead of opening another, so the register reads one row per concept
    and class with every source listed.
    """
    if diff_class is DiffClass.AGREE:
        raise ValueError("an agreement is not a register entry")
    status = "open" if diff_class is DiffClass.CONTRADICTION else "noted"
    cur.execute(
        """SELECT id FROM contradiction_register WHERE tenant_id=%s AND deal_id=%s
        AND concept_path=%s AND diff_class=%s AND status=%s ORDER BY created_at LIMIT 1 FOR UPDATE""",
        (tenant_id, deal_id, concept_path, diff_class.value, status),
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            """UPDATE contradiction_register SET assertion_ids=ARRAY(
                SELECT DISTINCT unnest(assertion_ids || %s::uuid[]) ORDER BY 1),
                material = material OR %s
            WHERE tenant_id=%s AND id=%s""",
            (assertion_ids, material, tenant_id, row[0]),
        )
        return str(row[0])
    cur.execute(
        """INSERT INTO contradiction_register
        (tenant_id,deal_id,concept_path,assertion_ids,diff_class,material,status,registry_version,created_at)
        VALUES(%s,%s,%s,%s::uuid[],%s,%s,%s,%s,%s) RETURNING id""",
        (
            tenant_id,
            deal_id,
            concept_path,
            sorted(set(assertion_ids)),
            diff_class.value,
            material,
            status,
            registry_version,
            now,
        ),
    )
    return str(cur.fetchone()[0])


def _select_rows(cur: Any, where: str, params: tuple[Any, ...]) -> list[Contradiction]:
    # Identifiers are fixed constants; values are parameters.
    cur.execute(f"SELECT {','.join(_COLUMNS)} FROM contradiction_register WHERE {where}", params)
    return [_row(r) for r in cur.fetchall()]


def list_contradictions(
    tenant_id: str, deal_id: str, *, status: str | None = None, cur: Any = None
) -> list[Contradiction]:
    """The deal's register, oldest first: open, resolved and noted rows alike."""
    where = "tenant_id=%s AND deal_id=%s" + (" AND status=%s" if status else "") + " ORDER BY created_at, id"
    params: tuple[Any, ...] = (tenant_id, deal_id, status) if status else (tenant_id, deal_id)
    if cur is not None:
        return _select_rows(cur, where, params)
    with tenant_cursor(tenant_id) as own:
        return _select_rows(own, where, params)


def read_register(cur: Any, tenant_id: str, deal_id: str, status: str | None) -> list[dict[str, Any]]:
    """Read the tenant-scoped register and source summaries through its stored procedure."""
    if status not in {None, "open", "resolved", "noted"}:
        raise ValueError("Invalid contradiction status")
    cur.execute(
        "SELECT public.get_deal_contradictions(%s::uuid,%s::text,%s::text)",
        (tenant_id, deal_id, status),
    )
    row = cur.fetchone()
    if not row:
        raise NotFound("Deal not found")
    payload = row[0] if row[0] else []
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, list):
        raise ValueError("Contradiction register response is invalid")
    return payload


def open_contradictions(cur: Any, tenant_id: str, deal_id: str) -> list[dict[str, Any]]:
    """Read open contradiction records and their source summaries for approval UI."""
    return read_register(cur, tenant_id, deal_id, "open")


def open_material_paths(cur: Any, tenant_id: str, deal_id: str, known_at: datetime) -> set[str]:
    """Concept paths held out of the projection: an open material contradiction, as known at
    ``known_at`` (recorded by then, not yet resolved by then)."""
    cur.execute(
        """SELECT DISTINCT concept_path FROM contradiction_register
        WHERE tenant_id=%s AND deal_id=%s AND diff_class='contradiction' AND material
          AND created_at<=%s AND (resolved_at IS NULL OR resolved_at>%s)""",
        (tenant_id, deal_id, known_at, known_at),
    )
    return {r[0] for r in cur.fetchall()}


def _close(
    cur: Any,
    tenant_id: str,
    row_id: str,
    *,
    value: Any,
    chosen_id: str,
    actor_id: str,
    reason: str,
    now: datetime,
) -> None:
    cur.execute(
        """UPDATE contradiction_register SET status='resolved',resolved_value=%s::jsonb,
        resolved_assertion_id=%s,resolved_by=%s,reason=%s,resolved_at=%s
        WHERE tenant_id=%s AND id=%s AND status='open'""",
        (
            json.dumps(value, sort_keys=True, default=str),
            chosen_id,
            actor_id,
            reason,
            now,
            tenant_id,
            row_id,
        ),
    )
    # The reason is kept in the row (the audit record); logs carry no figures or free text.
    log.info("contradiction %s resolved by %s at %s", row_id, actor_id, now.isoformat())


def _value_of(cur: Any, tenant_id: str, assertion_id: str) -> Any:
    cur.execute("SELECT fact FROM ledger_assertions WHERE tenant_id=%s AND id=%s", (tenant_id, assertion_id))
    payload = cur.fetchone()[0]
    payload = payload if isinstance(payload, dict) else json.loads(payload)
    return payload.get("value")


def settle(
    cur: Any,
    tenant_id: str,
    deal_id: str,
    assertion_id: str,
    *,
    actor_id: str,
    reason: str,
    now: datetime,
    chosen_id: str | None = None,
    chosen_value: Any = None,
) -> None:
    """Close open contradictions a Fact Sheet decision on ``assertion_id`` settles.

    - Accepting an assertion (``chosen_id`` given) resolves every open row that lists
      it, with the accepted value. A correction passes the analyst's new assertion.
    - Setting one aside resolves a row only when a single listed source is left
      standing, in that source's favour.

    Writes no label: the decision that calls this writes its own.
    """
    cur.execute(
        """SELECT id, assertion_ids FROM contradiction_register WHERE tenant_id=%s AND deal_id=%s
        AND status='open' AND %s::uuid = ANY(assertion_ids) FOR UPDATE""",
        (tenant_id, deal_id, assertion_id),
    )
    for row_id, ids in cur.fetchall():
        winner, value = chosen_id, chosen_value
        if winner is None:
            cur.execute(
                """SELECT a FROM unnest(%s::uuid[]) a WHERE NOT EXISTS(SELECT 1 FROM ledger_reviews r
                WHERE r.tenant_id=%s AND r.assertion_id=a AND r.action='reject')""",
                ([str(i) for i in ids], tenant_id),
            )
            standing = [str(r[0]) for r in cur.fetchall()]
            if len(standing) != 1:
                continue
            winner = standing[0]
            value = _value_of(cur, tenant_id, winner)
        _close(
            cur,
            tenant_id,
            str(row_id),
            value=value,
            chosen_id=winner,
            actor_id=actor_id,
            reason=reason,
            now=now,
        )


def resolve(
    tenant_id: str,
    deal_id: str,
    contradiction_id: str,
    *,
    chosen_assertion_id: str,
    actor_id: str,
    reason: str,
    expected_revision: int | None = None,
    reason_code: str | None = None,
) -> DealModel:
    """Per-deal resolution (rule level 2): the analyst picks the source and states why.

    One transaction under the deal lock: the row is resolved with the chosen value, the
    chosen assertion is accepted, every other listed source not yet decided is set
    aside, and one label records the decision: `accepted` when the analyst kept the
    value authority would project, `other_source` when they chose another.
    """
    from creditpaper.ledger import service

    if not reason or not reason.strip():
        raise ValueError("a contradiction is resolved with a stated reason")
    with tenant_cursor(tenant_id) as cur:
        service.lock_deal(cur, tenant_id, deal_id, expected_revision)
        rows = _select_rows(
            cur, "tenant_id=%s AND deal_id=%s AND id=%s FOR UPDATE", (tenant_id, deal_id, contradiction_id)
        )
        if not rows:
            raise NotFound("Contradiction not found")
        row = rows[0]
        if row.status != "open":
            raise Conflict("Only an open contradiction can be resolved")
        if chosen_assertion_id not in row.assertion_ids:
            raise Conflict("The chosen source is not part of this contradiction")
        now = service.clock(cur)
        presented_id = service.authority_choice(cur, tenant_id, deal_id, row.concept_path, row.assertion_ids)
        cur.execute(
            "SELECT id::text, fact FROM ledger_assertions WHERE tenant_id=%s AND id = ANY(%s::uuid[])",
            (tenant_id, list(row.assertion_ids)),
        )
        payloads = {aid: (p if isinstance(p, dict) else json.loads(p)) for aid, p in cur.fetchall()}
        chosen_value = payloads[chosen_assertion_id].get("value")
        _close(
            cur,
            tenant_id,
            row.id,
            value=chosen_value,
            chosen_id=chosen_assertion_id,
            actor_id=actor_id,
            reason=reason,
            now=now,
        )
        cur.execute(
            "SELECT assertion_id::text FROM ledger_reviews "
            "WHERE tenant_id=%s AND assertion_id = ANY(%s::uuid[])",
            (tenant_id, list(row.assertion_ids)),
        )
        reviewed = {r[0] for r in cur.fetchall()}
        for aid in row.assertion_ids:
            if aid != chosen_assertion_id and aid not in reviewed:
                service._review_one(cur, tenant_id, deal_id, aid, actor_id, "reject", reason, None, now)
        if chosen_assertion_id not in reviewed:
            service._review_one(
                cur, tenant_id, deal_id, chosen_assertion_id, actor_id, "accept", reason, None, now
            )
        service._label(
            cur,
            tenant_id,
            deal_id,
            row.concept_path,
            payloads[presented_id],
            action="accepted" if presented_id == chosen_assertion_id else "other_source",
            final_value=chosen_value,
            assertion_id=chosen_assertion_id,
            actor_id=actor_id,
            now=now,
            reason_code=reason_code,
        )
        return service._refresh(cur, tenant_id, deal_id, now, str(uuid4()))
