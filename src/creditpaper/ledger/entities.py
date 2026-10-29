"""Tenant-scoped hard identity keys; fuzzy results require an explicit decision."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from creditpaper.db import tenant_cursor
from creditpaper.ledger.service import lock_deal
from creditpaper.ledger.types import BadInput, Conflict, NotFound, QueryScope


def normalize_key(namespace: str, value: str) -> tuple[str, str]:
    namespace = namespace.strip().upper()
    value = value.strip().upper()
    if namespace in {"AU:ABN", "AU:ACN"}:
        number = re.sub(r"[\s-]", "", value)
        length = 11 if namespace == "AU:ABN" else 9
        if not re.fullmatch(rf"\d{{{length}}}", number) or len(set(number)) == 1:
            raise BadInput("Invalid registered identifier")
        digits = [int(c) for c in number]
        if namespace == "AU:ABN":
            digits[0] -= 1
            valid = (
                sum(d * w for d, w in zip(digits, [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19], strict=True)) % 89
                == 0
            )
        else:
            valid = (
                10 - sum(d * w for d, w in zip(digits[:8], range(8, 0, -1), strict=True)) % 10
            ) % 10 == digits[8]
        if not valid:
            raise BadInput("Invalid registered identifier checksum")
        return namespace, number
    if re.fullmatch(r"AU:(NSW|VIC|QLD|SA|WA|TAS|ACT|NT):(TITLE|LOT_PLAN)", namespace):
        if not re.fullmatch(r"[A-Z0-9 /.-]{1,100}", value):
            raise BadInput("Invalid property identifier")
        return namespace, re.sub(r"\s+", "", value)
    raise BadInput("Only registered or jurisdiction-qualified property identifiers are hard keys")


def require_evidence(cur, tenant_id: str, deal_id: str, assertion_id: str) -> None:
    cur.execute(
        "SELECT 1 FROM ledger_fact_history WHERE tenant_id=%s AND deal_id=%s AND assertion_id=%s "
        "AND superseded_at IS NULL",
        (tenant_id, deal_id, assertion_id),
    )
    if not cur.fetchone():
        raise NotFound("Evidence assertion not found")


def resolve_entity(
    tenant_id: str,
    *,
    name: str,
    kind: str,
    keys: list[tuple[str, str]],
    deal_id: str,
    assertion_id: str,
    actor_id: str,
) -> dict:
    normalized = [normalize_key(*key) for key in keys]
    if len({ns for ns, _ in normalized}) != len(normalized):
        raise BadInput("Provide at most one value per identifier namespace")
    with tenant_cursor(tenant_id) as cur:
        lock_deal(cur, tenant_id, deal_id)
        # Serialize registry changes per tenant, including same keys from different deals.
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("identity:" + tenant_id,))
        require_evidence(cur, tenant_id, deal_id, assertion_id)
        ids = set()
        for ns, value in normalized:
            cur.execute(
                "SELECT entity_id FROM ledger_identifiers WHERE tenant_id=%s AND namespace=%s AND value=%s",
                (tenant_id, ns, value),
            )
            row = cur.fetchone()
            if row:
                ids.add(str(row[0]))
        if len(ids) > 1:
            raise Conflict("Hard identifiers refer to different entities; human investigation required")
        if ids:
            entity_id = ids.pop()
            cur.execute(
                "SELECT kind FROM ledger_entities WHERE tenant_id=%s AND id=%s", (tenant_id, entity_id)
            )
            if cur.fetchone()[0] != kind:
                raise Conflict("Identifier matches a different entity type")
            # Never attach an unmatched identifier to an existing entity implicitly.
            for ns, value in normalized:
                cur.execute(
                    "SELECT 1 FROM ledger_identifiers WHERE tenant_id=%s AND namespace=%s AND value=%s",
                    (tenant_id, ns, value),
                )
                if not cur.fetchone():
                    raise Conflict("Additional identity key needs human confirmation")
            return {"entity_id": entity_id, "match": "hard_key", "proposals": []}
        cur.execute(
            "INSERT INTO ledger_entities(tenant_id,name,kind) VALUES(%s,%s,%s) RETURNING id",
            (tenant_id, name, kind),
        )
        entity_id = str(cur.fetchone()[0])
        for ns, value in normalized:
            cur.execute(
                """INSERT INTO ledger_identifiers
                (tenant_id,namespace,value,entity_id,source_deal_id,source_assertion_id,actor_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (tenant_id, ns, value, entity_id, deal_id, assertion_id, actor_id),
            )
        cur.execute(
            """SELECT id,name FROM ledger_entities WHERE tenant_id=%s AND kind=%s AND id<>%s
            AND lower(name) LIKE %s ORDER BY lower(name),id LIMIT 501""",
            (tenant_id, kind, entity_id, re.sub(r"[%_\\]", "", name[:1].lower()) + "%"),
        )
        candidates = cur.fetchall()
        proposals = []
        for other_id, other_name in candidates[:500]:
            score = SequenceMatcher(None, name.casefold(), other_name.casefold()).ratio()
            if score < 0.75:
                continue
            cur.execute(
                """INSERT INTO ledger_match_proposals(tenant_id,left_id,right_id,score)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
                (tenant_id, entity_id, other_id, score),
            )
            result = cur.fetchone()
            if result:
                proposals.append(
                    {
                        "proposal_id": str(result[0]),
                        "entity_id": str(other_id),
                        "name": other_name,
                        "score": score,
                        "requires_confirmation": True,
                    }
                )
        return {
            "entity_id": entity_id,
            "match": "new",
            "proposals": proposals,
            "candidate_search_limited": len(candidates) > 500,
        }


def confirm_match(tenant_id: str, proposal_id: str, actor_id: str, accepted: bool, reason: str) -> None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT 1 FROM ledger_match_proposals WHERE tenant_id=%s AND id=%s", (tenant_id, proposal_id)
        )
        if not cur.fetchone():
            raise NotFound("Match proposal not found")
        cur.execute(
            """INSERT INTO ledger_entity_links(tenant_id,proposal_id,actor_id,accepted,reason)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING proposal_id""",
            (tenant_id, proposal_id, actor_id, accepted, reason),
        )
        if not cur.fetchone():
            raise Conflict("Match proposal has already been decided")


def add_edge(
    tenant_id: str,
    *,
    source_id: str,
    target_id: str,
    kind: str,
    deal_id: str,
    assertion_id: str,
    actor_id: str,
    valid_from,
    valid_to=None,
) -> str:
    with tenant_cursor(tenant_id) as cur:
        lock_deal(cur, tenant_id, deal_id)
        require_evidence(cur, tenant_id, deal_id, assertion_id)
        for entity_id in (source_id, target_id):
            cur.execute("SELECT 1 FROM ledger_entities WHERE tenant_id=%s AND id=%s", (tenant_id, entity_id))
            if not cur.fetchone():
                raise NotFound("Entity not found")
        cur.execute(
            """INSERT INTO ledger_edges
            (tenant_id,source_id,target_id,kind,deal_id,assertion_id,actor_id,valid_from,valid_to)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (tenant_id, source_id, target_id, kind, deal_id, assertion_id, actor_id, valid_from, valid_to),
        )
        return str(cur.fetchone()[0])


def retract_match(tenant_id: str, proposal_id: str, actor_id: str, reason: str) -> None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT 1 FROM ledger_entity_links WHERE tenant_id=%s AND proposal_id=%s AND accepted",
            (tenant_id, proposal_id),
        )
        if not cur.fetchone():
            raise NotFound("Confirmed match not found")
        cur.execute(
            """INSERT INTO ledger_entity_link_retractions(tenant_id,proposal_id,actor_id,reason)
            VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING proposal_id""",
            (tenant_id, proposal_id, actor_id, reason),
        )
        if not cur.fetchone():
            raise Conflict("Match already retracted")


def retract_edge(tenant_id: str, edge_id: str, actor_id: str, reason: str) -> None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT 1 FROM ledger_edges WHERE tenant_id=%s AND id=%s", (tenant_id, edge_id))
        if not cur.fetchone():
            raise NotFound("Relationship not found")
        cur.execute(
            """INSERT INTO ledger_edge_retractions(tenant_id,edge_id,actor_id,reason)
            VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING edge_id""",
            (tenant_id, edge_id, actor_id, reason),
        )
        if not cur.fetchone():
            raise Conflict("Relationship already retracted")


def traverse(tenant_id: str, entity_id: str, scope: QueryScope, depth: int = 2) -> dict:
    if not 1 <= depth <= 4:
        raise BadInput("Traversal depth must be between 1 and 4")
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT 1 FROM ledger_entities WHERE tenant_id=%s AND id=%s AND recorded_at<=%s",
            (tenant_id, entity_id, scope.known_at),
        )
        if not cur.fetchone():
            raise NotFound("Entity not found at this knowledge time")
        cur.execute(
            """WITH RECURSIVE eligible AS (
              SELECT e.source_id,e.target_id,e.deal_id,e.kind FROM ledger_edges e
              WHERE e.tenant_id=%s AND e.recorded_at<=%s AND e.valid_from<=%s
                AND (e.valid_to IS NULL OR e.valid_to>%s)
                AND NOT EXISTS(SELECT 1 FROM ledger_edge_retractions r
                  WHERE r.tenant_id=e.tenant_id AND r.edge_id=e.id AND r.recorded_at<=%s)
              UNION ALL
              SELECT p.left_id,p.right_id,NULL,'same_entity' FROM ledger_match_proposals p
              JOIN ledger_entity_links l ON l.tenant_id=p.tenant_id AND l.proposal_id=p.id
              WHERE p.tenant_id=%s AND l.accepted AND l.recorded_at<=%s
              AND NOT EXISTS(SELECT 1 FROM ledger_entity_link_retractions r
                WHERE r.tenant_id=l.tenant_id AND r.proposal_id=l.proposal_id AND r.recorded_at<=%s)
            ), links AS (
              SELECT source_id,target_id,deal_id,kind FROM eligible UNION ALL
              SELECT target_id,source_id,deal_id,kind FROM eligible
            ), walk AS (
              SELECT %s::uuid AS node,0 AS depth,NULL::text AS deal_id
              UNION
              SELECT e.target_id,w.depth+1,e.deal_id
              FROM walk w JOIN links e ON e.source_id=w.node
              WHERE w.depth<%s
            ) SELECT DISTINCT node,deal_id FROM walk LIMIT 501""",
            (
                tenant_id,
                scope.known_at,
                scope.valid_at,
                scope.valid_at,
                scope.known_at,
                tenant_id,
                scope.known_at,
                scope.known_at,
                entity_id,
                depth,
            ),
        )
        rows = cur.fetchall()
        truncated = len(rows) > 500
        nodes = sorted({str(row[0]) for row in rows[:500]})
        deals = sorted({row[1] for row in rows[:500] if row[1]})
        # One row per distinct facility/deal, never one amount per graph path.
        exposures = []
        for deal_id in deals:
            from creditpaper.ledger.service import _history

            for fact in _history(cur, tenant_id, deal_id, scope):
                if fact["field_path"] == "economics.facility_amount":
                    exposures.append({"deal_id": deal_id, "fact": fact})
        return {
            "entity_ids": nodes,
            "deal_ids": deals,
            "exposures": exposures,
            "truncated": truncated,
            "scope": scope.model_dump(mode="json"),
        }
