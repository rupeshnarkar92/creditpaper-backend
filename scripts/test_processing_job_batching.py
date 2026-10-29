"""Dependency-light regressions for WAN round-trip batching."""

from __future__ import annotations

import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch


class _Cursor:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))

    def fetchone(self):
        return (self.result,)


@contextmanager
def _cursor_context(cursor):
    yield cursor


class ClaimBatchTests(unittest.TestCase):
    def test_one_claim_round_trip_feeds_eight_workers(self):
        from creditpaper.ledger import jobs

        claimed = [
            {
                "id": str(index),
                "lease": f"lease-{index}",
                "kind": "parse",
                "processing_input": {"version_id": str(index)},
            }
            for index in range(8)
        ]
        cursor = _Cursor(claimed)
        with jobs._PREFETCH_LOCK:
            jobs._PREFETCH.clear()
            jobs._PREFETCH_EMPTY_UNTIL.clear()
        with (
            patch.object(jobs, "tenant_cursor", return_value=_cursor_context(cursor)),
            patch.object(jobs, "_ensure_heartbeat_thread"),
        ):
            results = [jobs._claim_prefetched("tenant", jobs.FAST_INGESTION_KINDS) for _ in range(8)]
        self.assertEqual([item["id"] for item in results], [str(i) for i in range(8)])
        self.assertEqual(len(cursor.calls), 1)
        self.assertIn("claim_processing_jobs_prepared", cursor.calls[0][0])
        with jobs._ACTIVE_LEASES_LOCK:
            self.assertEqual(len(jobs._ACTIVE_LEASES), 8)
            jobs._ACTIVE_LEASES.clear()


class FinishBatchTests(unittest.TestCase):
    def test_concurrent_finishes_share_one_transaction(self):
        from creditpaper.processing import job_finish_batch as batcher

        class FinishCursor(_Cursor):
            def fetchone(self):
                items = self.calls[-1][1][1]
                import json

                return ([{"id": item["id"], "status": "complete"} for item in json.loads(items)],)

        cursor = FinishCursor(None)
        with batcher._CONDITION:
            batcher._QUEUE.clear()
            batcher._STARTED = False
        old_window = batcher._WINDOW_SECONDS
        batcher._WINDOW_SECONDS = 0.05
        statuses = []

        def finish(index):
            statuses.append(batcher.finish_job("tenant", {"id": str(index)}))

        try:
            with (
                patch.object(batcher, "tenant_cursor", return_value=_cursor_context(cursor)),
                patch.object(batcher, "worker_db_task", return_value=_cursor_context(None)),
            ):
                threads = [threading.Thread(target=finish, args=(index,)) for index in range(4)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(2)
        finally:
            batcher._WINDOW_SECONDS = old_window
        self.assertEqual(statuses, ["complete"] * 4)
        self.assertEqual(len(cursor.calls), 1)
        self.assertIn("finish_processing_jobs_observed_batch", cursor.calls[0][0])


if __name__ == "__main__":
    unittest.main()
