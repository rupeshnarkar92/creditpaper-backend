"""Raw psycopg queries for the guardrails domain.

Same pattern as templates/store and onboarding/utils/store — raw SQL via
the shared connection pool, no SQLAlchemy.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from creditpaper.db import cursor, tenant_cursor, tenant_uuid

_COLS = (
    "id, tenant_id, kind, payload, severity, weight, "
    "scope_type, scope_value, is_active, created_at, updated_at"
)


def _t(tenant_id: str) -> str:
    return str(tenant_uuid(tenant_id))


def _row_to_dict(r: Any) -> dict[str, Any]:
    return {
        "id": str(r[0]),
        "tenant_id": str(r[1]),
        "kind": r[2],
        "payload": dict(r[3] or {}),
        "severity": r[4],
        "weight": float(r[5]),
        "scope_type": r[6],
        "scope_value": r[7],
        "is_active": bool(r[8]),
        "created_at": r[9],
        "updated_at": r[10],
    }


def list_all(
    tenant_id: str,
) -> list[dict[str, Any]]:
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT * FROM public.guardrail_rules_all(%s)", (t,))
        return [_row_to_dict(r) for r in cur.fetchall()]


def list_all_rules(
    *,
    tenant_id: str | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
    order_by: str = "-updated_at",
) -> tuple[list[dict[str, Any]], int]:
    """Superadmin view: every tenant's guardrail rules, newest first.

    `tenant_id` is a filter, never an access check — the caller is already
    gated by require_superadmin. Raises ValueError on a non-UUID tenant id.
    """
    where = ["1=1"]
    params: list[Any] = []
    if tenant_id:
        where.append("tenant_id = %s")
        params.append(_t(tenant_id))
    if q:
        where.append(
            "(kind ILIKE %s OR COALESCE(scope_type, '') ILIKE %s OR COALESCE(scope_value, '') ILIKE %s)"
        )
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    clause = " AND ".join(where)
    descending = order_by.startswith("-")
    sort_key = order_by[1:] if descending else order_by
    sort_column = {
        "kind": "kind",
        "severity": "severity",
        "scope_type": "scope_type",
        "is_active": "is_active",
        "created_at": "created_at",
        "updated_at": "updated_at",
    }.get(sort_key, "updated_at")
    direction = "DESC" if descending else "ASC"
    with cursor() as cur:
        cur.execute(f"SELECT count(*) FROM guardrail_rules WHERE {clause}", params)
        total = int(cur.fetchone()[0])
        cur.execute(
            f"SELECT {_COLS} FROM guardrail_rules WHERE {clause} "
            f"ORDER BY {sort_column} {direction}, id DESC LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows], total


def list_active(tenant_id: str, module: str | None = None) -> list[dict[str, Any]]:
    """Active rules visible to a generation context.

    A rule applies when it is global, or when it is scoped to `module`.
    Passing module=None returns every active rule.
    """
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        if module is None:
            cur.execute("SELECT * FROM public.guardrail_rules_active(%s,NULL)", (t,))
        else:
            cur.execute("SELECT * FROM public.guardrail_rules_active(%s,%s)", (t, module))
        return [_row_to_dict(r) for r in cur.fetchall()]


def get_by_id(tenant_id: str, rule_id: str) -> dict[str, Any] | None:
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT * FROM public.guardrail_rule_row(%s,%s)", (t, rule_id))
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def create(
    tenant_id: str,
    *,
    kind: str,
    payload: dict[str, Any],
    severity: str = "info",
    weight: float = 1.0,
    scope_type: str = "global",
    scope_value: str | None = None,
) -> dict[str, Any]:
    t = _t(tenant_id)
    new_id = str(uuid.uuid4())
    with tenant_cursor(t) as cur:
        cur.execute(
            "SELECT * FROM public.create_guardrail_rule(%s,%s,%s,%s::jsonb,%s,%s,%s,%s)",
            (new_id, t, kind, json.dumps(payload), severity, weight, scope_type, scope_value),
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row else {}


def update(tenant_id: str, rule_id: str, **fields: Any) -> dict[str, Any] | None:
    t = _t(tenant_id)
    allowed = {"payload", "severity", "weight", "scope_type", "scope_value", "is_active"}
    if not fields or not (set(fields) & allowed):
        return get_by_id(tenant_id, rule_id)
    values = (
        json.dumps(fields["payload"]) if "payload" in fields else None,
        fields.get("severity"),
        fields.get("weight"),
        fields.get("scope_type"),
        fields.get("scope_value"),
        fields.get("is_active"),
    )
    with tenant_cursor(t) as cur:
        cur.execute(
            "SELECT * FROM public.update_guardrail_rule(%s,%s,%s::jsonb,%s,%s,%s,%s,%s)",
            (t, rule_id, *values),
        )
        row = cur.fetchone()
    return _row_to_dict(row) if row else None


def deactivate(tenant_id: str, rule_id: str) -> bool:
    """Soft-delete: keep the row for audit, flip is_active off."""
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT public.deactivate_guardrail_rule(%s,%s)", (t, rule_id))
        return bool(cur.fetchone()[0])


def record_evaluation(
    tenant_id: str,
    *,
    rule_id: str,
    subject_type: str,
    subject_id: str,
    result: str,
    measured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    t = _t(tenant_id)
    new_id = str(uuid.uuid4())
    with tenant_cursor(t) as cur:
        cur.execute(
            "SELECT * FROM public.record_guardrail_evaluation(%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (new_id, t, rule_id, subject_type, subject_id, result, json.dumps(measured or {})),
        )
        r = cur.fetchone()
    return {
        "id": str(r[0]),
        "rule_id": str(r[2]),
        "subject_type": r[3],
        "subject_id": str(r[4]),
        "result": r[5],
        "measured": dict(r[6] or {}),
        "evaluated_at": r[7].isoformat() if r[7] else None,
    }


def list_evaluations(tenant_id: str, rule_id: str, limit: int = 100) -> list[dict[str, Any]]:
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute("SELECT * FROM public.guardrail_evaluations(%s,%s,%s)", (t, rule_id, limit))
        out = []
        for r in cur.fetchall():
            out.append(
                {
                    "id": str(r[0]),
                    "rule_id": str(r[2]),
                    "subject_type": r[3],
                    "subject_id": str(r[4]),
                    "result": r[5],
                    "measured": dict(r[6] or {}),
                    "evaluated_at": r[7].isoformat() if r[7] else None,
                }
            )
    return out


def accuracy(tenant_id: str) -> dict[str, Any]:
    """Per-rule pass rates plus the weight-averaged overall score.

    Informational only — scores never gate approval.
    """
    t = _t(tenant_id)
    with tenant_cursor(t) as cur:
        cur.execute(
            "SELECT * FROM public.guardrail_accuracy_rows(%s)",
            (t,),
        )
        rows = cur.fetchall()
    rules = []
    weighted_pass = 0.0
    weighted_total = 0.0
    total_evals = 0
    for rule_id, kind, weight, total, passes, flags in rows:
        w = float(weight)
        rate = (passes / total) if total else None
        rules.append(
            {
                "rule_id": str(rule_id),
                "kind": kind,
                "evaluations": total,
                "passes": passes,
                "flags": flags,
                "pass_rate": rate,
            }
        )
        if total:
            weighted_pass += passes * w
            weighted_total += total * w
            total_evals += total
    return {
        "overall_score": (weighted_pass / weighted_total) if weighted_total else None,
        "total_evaluations": total_evals,
        "rules": rules,
    }
