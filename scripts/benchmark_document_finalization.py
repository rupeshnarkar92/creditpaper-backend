"""Rollback-only benchmark for the durable document finalization path.

This script replays already-persisted extraction payloads against the real
PostgreSQL function. Every sample runs in a force-rollback transaction: lease,
document, cache, and audit state are unchanged after the benchmark.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from creditpaper.db import cursor, get_pool, tenant_cursor
from creditpaper.processing.store import PARSER_VERSION


@dataclass(frozen=True)
class Candidate:
    version_id: str
    job_id: str
    content_hash: str
    payload: dict[str, Any]


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _representative(rows: list[tuple[Any, ...]], count: int) -> list[tuple[Any, ...]]:
    if len(rows) < count:
        raise RuntimeError(f"Need {count} finalizable documents; found {len(rows)}")
    if count == 1:
        return [rows[len(rows) // 2]]
    return [rows[round(index * (len(rows) - 1) / (count - 1))] for index in range(count)]


def _load_candidates(deal_id: str, count: int) -> tuple[str, list[Candidate]]:
    with cursor() as cur:
        cur.execute("SELECT tenant_id::text FROM public.deals WHERE id=%s", (deal_id,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"Deal not found: {deal_id}")
    tenant_id = str(row[0])

    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """
            SELECT e.version_id::text,j.id::text,v.content_sha256,e.full_text,
                   e.page_texts,e.chunks,e.page_count,e.sheet_count,e.char_count,
                   e.classification,octet_length(e.full_text)
            FROM public.deal_document_extractions e
            JOIN public.deal_document_versions v
              ON v.tenant_id=e.tenant_id AND v.id=e.version_id
            JOIN LATERAL (
              SELECT id FROM public.processing_jobs
              WHERE tenant_id=e.tenant_id AND deal_id=e.deal_id
                AND resource_id=e.version_id::text AND kind IN ('parse','parse_ocr')
              ORDER BY updated_at DESC LIMIT 1
            ) j ON true
            WHERE e.tenant_id=%s::uuid AND e.deal_id=%s
            ORDER BY octet_length(e.full_text),e.version_id
            """,
            (tenant_id, deal_id),
        )
        selected = _representative(cur.fetchall(), count)
        version_ids = [row[0] for row in selected]
        cur.execute(
            """
            SELECT version_id::text,page,location,chunk_index,content,content_hash
            FROM public.ledger_chunks
            WHERE tenant_id=%s::uuid AND version_id=ANY(%s::uuid[])
            ORDER BY version_id,chunk_index
            """,
            (tenant_id, version_ids),
        )
        index_chunks: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for version_id, page, location, chunk_index, content, content_hash in cur.fetchall():
            index_chunks[str(version_id)].append(
                {
                    "page": page,
                    "location": location,
                    "chunk_index": chunk_index,
                    "content": content,
                    "content_hash": content_hash,
                }
            )

    candidates = []
    for row in selected:
        version_id, job_id, content_hash = row[:3]
        payload = {
            "full_text": row[3],
            "page_texts": row[4],
            "chunks": row[5],
            "page_count": row[6],
            "sheet_count": row[7],
            "char_count": row[8],
            "classification": row[9],
            "index_chunks": index_chunks[version_id],
        }
        candidates.append(Candidate(version_id, job_id, content_hash, payload))
    return tenant_id, candidates


def _sample(
    tenant_id: str, deal_id: str, candidates: list[Candidate], *, pipelined: bool
) -> dict[str, float]:
    items = []
    leases = []
    for candidate in candidates:
        lease = str(uuid.uuid4())
        leases.append({"id": candidate.job_id, "lease": lease})
        items.append(
            {
                "job_id": candidate.job_id,
                "lease": lease,
                "deal_id": deal_id,
                "version_id": candidate.version_id,
                "content_hash": candidate.content_hash,
                "parser_version": PARSER_VERSION,
                "payload": candidate.payload,
            }
        )
    encoded = json.dumps(items, separators=(",", ":"))

    total_started = time.perf_counter()
    acquire_started = time.perf_counter()
    with get_pool().connection() as conn:
        acquire_ms = (time.perf_counter() - acquire_started) * 1000
        if pipelined:
            pipeline_started = time.perf_counter()
            with conn.cursor() as setup_cur, conn.cursor() as lease_cur, conn.cursor() as work_cur:
                with conn.pipeline():
                    with conn.transaction(force_rollback=True):
                        setup_cur.execute(
                            """SELECT set_config('app.tenant_id',%s,true),
                                      set_config('statement_timeout','30s',true),
                                      set_config('lock_timeout','5s',true),
                                      set_config('idle_in_transaction_session_timeout','30s',true)""",
                            (tenant_id,),
                        )
                        lease_cur.execute(
                            """
                            UPDATE public.processing_jobs j
                            SET status='running',lease_token=x.lease,
                                lease_until=clock_timestamp()+interval '60 seconds'
                            FROM jsonb_to_recordset(%s::jsonb) AS x(id uuid,lease uuid)
                            WHERE j.tenant_id=%s::uuid AND j.id=x.id
                            """,
                            (json.dumps(leases), tenant_id),
                        )
                        work_cur.execute(
                            """EXPLAIN (ANALYZE,BUFFERS,WAL,FORMAT JSON)
                               SELECT public.finish_document_processing_batch(%s::uuid,%s::jsonb)""",
                            (tenant_id, encoded),
                        )
                plan = work_cur.fetchone()[0][0]
            pipeline_ms = (time.perf_counter() - pipeline_started) * 1000
            return {
                "acquire_ms": acquire_ms,
                "pipeline_ms": pipeline_ms,
                "server_execution_ms": float(plan["Execution Time"]),
                "server_planning_ms": float(plan["Planning Time"]),
                "pipeline_minus_server_ms": max(
                    0.0, pipeline_ms - float(plan["Execution Time"])
                ),
                "total_ms": (time.perf_counter() - total_started) * 1000,
                "payload_bytes": float(len(encoded.encode("utf-8"))),
            }
        rollback_started = 0.0
        with conn.transaction(force_rollback=True):
            with conn.cursor() as cur:
                setup_started = time.perf_counter()
                cur.execute(
                    """SELECT set_config('app.tenant_id',%s,true),
                              set_config('statement_timeout','30s',true),
                              set_config('lock_timeout','5s',true),
                              set_config('idle_in_transaction_session_timeout','30s',true)""",
                    (tenant_id,),
                )
                tenant_setup_ms = (time.perf_counter() - setup_started) * 1000
                cur.execute(
                    """
                    UPDATE public.processing_jobs j
                    SET status='running',lease_token=x.lease,lease_until=clock_timestamp()+interval '60 seconds'
                    FROM jsonb_to_recordset(%s::jsonb) AS x(id uuid,lease uuid)
                    WHERE j.tenant_id=%s::uuid AND j.id=x.id
                    """,
                    (json.dumps(leases), tenant_id),
                )
                execute_started = time.perf_counter()
                cur.execute(
                    """EXPLAIN (ANALYZE,BUFFERS,WAL,FORMAT JSON)
                       SELECT public.finish_document_processing_batch(%s::uuid,%s::jsonb)""",
                    (tenant_id, encoded),
                )
                plan = cur.fetchone()[0][0]
                client_query_ms = (time.perf_counter() - execute_started) * 1000
            rollback_started = time.perf_counter()
        rollback_ms = (time.perf_counter() - rollback_started) * 1000
    total_ms = (time.perf_counter() - total_started) * 1000
    plan_node = plan["Plan"]
    measured_ms = acquire_ms + tenant_setup_ms + client_query_ms + rollback_ms
    return {
        "acquire_ms": acquire_ms,
        "tenant_setup_ms": tenant_setup_ms,
        "client_query_ms": client_query_ms,
        "server_execution_ms": float(plan["Execution Time"]),
        "server_planning_ms": float(plan["Planning Time"]),
        "network_and_client_ms": max(0.0, client_query_ms - float(plan["Execution Time"])),
        "rollback_ms": rollback_ms,
        "total_ms": total_ms,
        "benchmark_lease_setup_and_release_ms": max(0.0, total_ms - measured_ms),
        "payload_bytes": float(len(encoded.encode("utf-8"))),
        "wal_records": float(plan_node.get("WAL Records", 0)),
        "wal_bytes": float(plan_node.get("WAL Bytes", 0)),
        "shared_hit_blocks": float(plan_node.get("Shared Hit Blocks", 0)),
        "shared_dirtied_blocks": float(plan_node.get("Shared Dirtied Blocks", 0)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deal", required=True)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--batch-sizes", default="1,2,4,8")
    parser.add_argument("--modes", default="conventional,pipelined")
    args = parser.parse_args()
    batch_sizes = [int(value) for value in args.batch_sizes.split(",")]
    modes = [value.strip() for value in args.modes.split(",")]
    if args.iterations < 1 or not batch_sizes or min(batch_sizes) < 1 or max(batch_sizes) > 8:
        raise SystemExit("iterations must be positive and batch sizes must be between 1 and 8")
    if not modes or any(mode not in {"conventional", "pipelined"} for mode in modes):
        raise SystemExit("modes must contain conventional and/or pipelined")

    tenant_id, _ = _load_candidates(args.deal, 1)
    print(
        json.dumps(
            {
                "event": "benchmark_start",
                "deal_id": args.deal,
                "tenant_id": tenant_id,
                "iterations": args.iterations,
                "batch_sizes": batch_sizes,
                "rollback_only": True,
            }
        )
    )
    for mode in modes:
        for batch_size in batch_sizes:
            sample_tenant_id, candidates = _load_candidates(args.deal, batch_size)
            if sample_tenant_id != tenant_id:
                raise RuntimeError("deal tenant changed during benchmark")
            samples = [
                _sample(
                    tenant_id,
                    args.deal,
                    candidates[:batch_size],
                    pipelined=mode == "pipelined",
                )
                for _ in range(args.iterations)
            ]
            result: dict[str, Any] = {
                "event": "benchmark_result",
                "mode": mode,
                "batch_size": batch_size,
            }
            for metric in samples[0]:
                values = [sample[metric] for sample in samples]
                result[metric] = {
                    "p50": round(statistics.median(values), 2),
                    "p95": round(_percentile(values, 0.95), 2),
                    "max": round(max(values), 2),
                }
            result["client_statements_per_batch"] = 1
            result["client_statements_per_document"] = round(1 / batch_size, 3)
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
