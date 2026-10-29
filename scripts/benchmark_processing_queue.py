"""Report privacy-safe queue and stage latency percentiles for one tenant.

No filenames, document text, prompts, facts, or user identifiers are selected.
"""

from __future__ import annotations

import argparse
import json

from creditpaper.db import tenant_cursor, tenant_uuid


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--deal", help="Limit measurements to one fresh benchmark deal")
    args = parser.parse_args()
    tenant = str(tenant_uuid(args.tenant))
    if not 1 <= args.hours <= 24 * 30:
        parser.error("--hours must be between 1 and 720")
    with tenant_cursor(tenant) as cur:
        cur.execute(
            """SELECT kind,status,count(*)::integer,
              round(percentile_cont(.5) WITHIN GROUP(ORDER BY
                EXTRACT(epoch FROM (updated_at-created_at))*1000))::bigint AS p50_total_ms,
              round(percentile_cont(.95) WITHIN GROUP(ORDER BY
                EXTRACT(epoch FROM (updated_at-created_at))*1000))::bigint AS p95_total_ms,
              round(percentile_cont(.95) WITHIN GROUP(ORDER BY
                COALESCE((stage_timings->>'queue_wait')::numeric,0)))::bigint AS p95_queue_ms
            FROM public.processing_jobs
            WHERE tenant_id=%s::uuid AND created_at>=now()-(%s*interval '1 hour')
              AND (%s::text IS NULL OR deal_id=%s)
            GROUP BY kind,status ORDER BY kind,status""",
            (tenant, args.hours, args.deal, args.deal),
        )
        jobs = [
            {
                "kind": row[0],
                "status": row[1],
                "count": row[2],
                "p50_total_ms": row[3],
                "p95_total_ms": row[4],
                "p95_queue_ms": row[5],
            }
            for row in cur.fetchall()
        ]
        cur.execute(
            """SELECT key,
              round(percentile_cont(.5) WITHIN GROUP(ORDER BY value::numeric))::bigint,
              round(percentile_cont(.95) WITHIN GROUP(ORDER BY value::numeric))::bigint
            FROM public.processing_jobs j
            CROSS JOIN LATERAL jsonb_each_text(j.stage_timings)
            WHERE tenant_id=%s::uuid AND created_at>=now()-(%s*interval '1 hour')
              AND (%s::text IS NULL OR deal_id=%s)
              AND value ~ '^[0-9]+$'
            GROUP BY key ORDER BY key""",
            (tenant, args.hours, args.deal, args.deal),
        )
        stages = {row[0]: {"p50_ms": row[1], "p95_ms": row[2]} for row in cur.fetchall()}
        cur.execute(
            """SELECT
              COALESCE(round(EXTRACT(epoch FROM(clock_timestamp()-min(created_at)))*1000),0)::bigint,
              count(*)::integer
            FROM public.processing_jobs
            WHERE tenant_id=%s::uuid AND status IN ('pending','running')
              AND (%s::text IS NULL OR deal_id=%s)""",
            (tenant, args.deal, args.deal),
        )
        oldest_job_age_ms, active_jobs = cur.fetchone()
        cur.execute(
            """WITH deal_runs AS (
              SELECT deal_id,
                EXTRACT(epoch FROM(max(updated_at)-min(created_at)))*1000 AS elapsed_ms
              FROM public.processing_jobs
              WHERE tenant_id=%s::uuid AND created_at>=now()-(%s*interval '1 hour')
                AND (%s::text IS NULL OR deal_id=%s)
                AND kind IN ('archive_import','parse','parse_ocr','archive_match')
              GROUP BY deal_id
              HAVING bool_and(status IN ('complete','failed'))
            )
            SELECT count(*)::integer,
              round(percentile_cont(.5) WITHIN GROUP(ORDER BY elapsed_ms))::bigint,
              round(percentile_cont(.95) WITHIN GROUP(ORDER BY elapsed_ms))::bigint,
              round(max(elapsed_ms))::bigint
            FROM deal_runs""",
            (tenant, args.hours, args.deal, args.deal),
        )
        completed_deals, ingestion_p50_ms, ingestion_p95_ms, ingestion_max_ms = cur.fetchone()
        cur.execute(
            """WITH runs AS (
              SELECT deal_id, EXTRACT(epoch FROM (
                max(updated_at) FILTER (WHERE kind='generation' AND status='complete')
                - min(created_at))) * 1000 AS elapsed_ms
              FROM public.processing_jobs
              WHERE tenant_id=%s::uuid AND created_at>=now()-(%s*interval '1 hour')
                AND (%s::text IS NULL OR deal_id=%s)
              GROUP BY deal_id
              HAVING bool_and(status IN ('complete','failed'))
                AND bool_or(kind='generation' AND status='complete')
                AND bool_or(kind IN ('ingest_speculate','ingest_window','corpus_extraction',
                                     'extraction','document') AND status='complete')
            ) SELECT count(*)::integer,
                round(percentile_cont(.5) WITHIN GROUP(ORDER BY elapsed_ms))::bigint,
                round(percentile_cont(.95) WITHIN GROUP(ORDER BY elapsed_ms))::bigint
              FROM runs""",
            (tenant, args.hours, args.deal, args.deal),
        )
        full_count, full_p50, full_p95 = cur.fetchone()
    print(
        json.dumps(
            {
                "window_hours": args.hours,
                "timing_origin": "first durable job; excludes browser upload time",
                "scope": "selected deal" if args.deal else "historical per-deal lifetimes",
                "full_workflow": {
                    "completed_deals_with_facts_and_generation": full_count,
                    "p50_ms": full_p50,
                    "p95_ms": full_p95,
                },
                "queue": {
                    "active_jobs": active_jobs,
                    "oldest_job_age_ms": oldest_job_age_ms,
                },
                "ingestion": {
                    "completed_deals": completed_deals,
                    "p50_ms": ingestion_p50_ms,
                    "p95_ms": ingestion_p95_ms,
                    "max_ms": ingestion_max_ms,
                },
                "jobs": jobs,
                "stages": stages,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
