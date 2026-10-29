"""Print a content-free durable timing snapshot for one benchmark deal."""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime

from creditpaper.db import cursor


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def main() -> None:
    deal_id = sys.argv[1]
    with cursor() as cur:
        cur.execute("SELECT tenant_id FROM deals WHERE id=%s", (deal_id,))
        tenant_row = cur.fetchone()
        if not tenant_row:
            raise SystemExit(f"deal not found: {deal_id}")
        tenant_id = tenant_row[0]
        cur.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        cur.execute(
            """SELECT kind,status,attempts,created_at,updated_at,stage_timings,
                      COALESCE(result->>'error_type',result->>'error')
               FROM processing_jobs WHERE deal_id=%s ORDER BY created_at,id""",
            (deal_id,),
        )
        jobs = cur.fetchall()
        cur.execute(
            """SELECT v.processing_status::text,count(*) FROM deal_document_versions v
               JOIN deal_documents d ON d.id=v.document_id AND d.tenant_id=v.tenant_id
               WHERE v.deal_id=%s AND v.superseded_at IS NULL AND d.archived_at IS NULL
               GROUP BY v.processing_status""",
            (deal_id,),
        )
        documents = dict(cur.fetchall())
        cur.execute(
            """SELECT count(*) FROM ledger_assertions WHERE deal_id=%s""",
            (deal_id,),
        )
        facts = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM deal_paper_sections WHERE deal_id=%s", (deal_id,))
        sections = cur.fetchone()[0]
        cur.execute(
            "SELECT public.deal_ingestion_accounting(%s::uuid,%s)",
            (tenant_id, deal_id),
        )
        accounting = cur.fetchone()[0]

    status = Counter((row[0], row[1]) for row in jobs)
    timings: dict[str, list[int]] = defaultdict(list)
    for _kind, _status, _attempts, _created, _updated, stage_timings, _error in jobs:
        for stage, elapsed in (stage_timings or {}).items():
            if isinstance(elapsed, (int, float)):
                timings[stage].append(int(elapsed))
    first = min((row[3] for row in jobs), default=None)
    last = max((row[4] for row in jobs if row[1] in {"complete", "failed"}), default=None)
    all_terminal = bool(jobs) and all(row[1] in {"complete", "failed"} for row in jobs)
    print(
        json.dumps(
            {
                "deal_id": deal_id,
                "jobs": {f"{kind}:{state}": count for (kind, state), count in sorted(status.items())},
                "documents": documents,
                "accounting": accounting,
                "facts": facts,
                "paper_sections": sections,
                "attempts_over_one": sum(max(0, row[2] - 1) for row in jobs),
                "errors": dict(Counter(row[6] for row in jobs if row[6])),
                "durable_elapsed_s": round((last - first).total_seconds(), 3) if first and last else None,
                "wall_elapsed_s": round((datetime.now(UTC) - first).total_seconds(), 3) if first else None,
                "all_jobs_terminal": all_terminal,
                "stage_ms": {
                    stage: {
                        "count": len(values),
                        "p50": round(statistics.median(values)),
                        "p95": percentile(values, 0.95),
                        "max": max(values),
                    }
                    for stage, values in sorted(timings.items())
                },
            },
            default=str,
        )
    )


if __name__ == "__main__":
    main()
