"""Persistence for generation runs, their stage outputs and their spend (spec §12).

Stage outputs are keyed by a cache key (revision digest + skill versions +
lesson set + guardrail snapshot + harness version + effort + upstream hash,
built by the pipeline) so a crash resumes at the failed stage and an unchanged
stage is reused. Each output is written once per (run, stage, unit).

Spend is separate from outputs: every call is recorded, failed and retried calls
included, because each was billed. The per-run cost ceiling reads `run_spend`,
so it survives a crash and never undercounts a stage that took three attempts.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from creditpaper.contracts.generation import RunVerdict
from creditpaper.db import tenant_cursor


def start_run(
    tenant_id: str,
    deal_id: str,
    *,
    ledger_revision: int,
    fact_digest: str,
    harness_version: str,
    job_id: str | None = None,
) -> str:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """INSERT INTO generation_runs
                (tenant_id, deal_id, job_id, ledger_revision, fact_digest, harness_version)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (tenant_id, deal_id, job_id, ledger_revision, fact_digest, harness_version),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("generation run insert returned no id")
    return str(row[0])


def record_stage_output(
    tenant_id: str,
    run_id: str,
    *,
    stage: str,
    unit: str,
    cache_key: str,
    output: dict[str, Any],
) -> None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """INSERT INTO generation_stage_outputs (tenant_id, run_id, stage, unit, cache_key, output)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (tenant_id, run_id, stage, unit) DO NOTHING""",
            (tenant_id, run_id, stage, unit, cache_key, json.dumps(output, sort_keys=True)),
        )


def record_spend(
    tenant_id: str,
    run_id: str,
    *,
    stage: str,
    unit: str,
    cost_usd: Decimal,
    failed: bool = False,
) -> None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """INSERT INTO generation_spend (tenant_id, run_id, stage, unit, cost_usd, failed)
            VALUES (%s, %s, %s, %s, %s, %s)""",
            (tenant_id, run_id, stage, unit, cost_usd, failed),
        )


def find_cached_output(tenant_id: str, cache_key: str) -> dict[str, Any] | None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """SELECT output FROM generation_stage_outputs
            WHERE tenant_id = %s AND cache_key = %s ORDER BY created_at DESC LIMIT 1""",
            (tenant_id, cache_key),
        )
        row = cur.fetchone()
    return dict(row[0]) if row else None


def run_spend(tenant_id: str, run_id: str) -> Decimal:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM generation_spend WHERE tenant_id = %s AND run_id = %s",
            (tenant_id, run_id),
        )
        row = cur.fetchone()
    return Decimal(row[0]) if row else Decimal("0")


def finish_run(
    tenant_id: str, run_id: str, *, verdict: RunVerdict, manifest: dict[str, Any], cost_usd: Decimal
) -> None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """UPDATE generation_runs SET verdict = %s, manifest = %s::jsonb, cost_usd = %s,
            finished_at = clock_timestamp() WHERE tenant_id = %s AND id = %s""",
            (verdict.value, json.dumps(manifest, sort_keys=True), cost_usd, tenant_id, run_id),
        )
