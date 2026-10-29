"""Dependency-light regression checks for production worker sizing."""

from __future__ import annotations

import unittest
from unittest import mock

from process_jobs import configure_lane_batches

from creditpaper.concurrency import pool
from creditpaper.db import _WORKER_TASK_ACTIVE, assert_pool_budget, worker_db_task
from creditpaper.ledger.capacity import validate_worker_capacity
from creditpaper.ledger.jobs import processing_stage, stage_checkpoint


class WorkerCapacityTests(unittest.TestCase):
    def test_database_budget_counts_supervised_worker_processes(self) -> None:
        assert_pool_budget(8, 2, 2)
        with self.assertRaisesRegex(RuntimeError, r"WORKER\(3 x 2\)"):
            assert_pool_budget(8, 3, 2)

    def test_supported_ingestion_shape(self) -> None:
        result = validate_worker_capacity(
            lane="ingestion",
            concurrency=4,
            per_tenant=2,
            db_pool_max=5,
            cpu_count=8,
            ocr_workers=4,
            pdf_processes=4,
        )
        self.assertEqual(result.concurrency, 4)
        self.assertEqual(result.control_plane_connections, 1)

    def test_compute_concurrency_is_independent_of_db_pool(self) -> None:
        result = validate_worker_capacity(
            lane="all",
            concurrency=4,
            per_tenant=2,
            db_pool_max=2,
            cpu_count=8,
            ocr_workers=2,
            pdf_processes=2,
        )
        self.assertEqual(result.concurrency, 4)

    def test_split_document_lanes_are_supported(self) -> None:
        for lane in ("fast", "ocr"):
            result = validate_worker_capacity(
                lane=lane,
                concurrency=2,
                per_tenant=1,
                db_pool_max=2,
                cpu_count=8,
                ocr_workers=2,
                pdf_processes=2,
            )
            self.assertEqual(result.lane, lane)

    def test_single_ocr_worker_never_prefetches_extra_leases(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "PROCESSING_CLAIM_BATCH": "8",
                "EXTRACTION_FINALIZE_BATCH": "8",
                "PROCESSING_FINISH_BATCH": "8",
            },
        ):
            limits = configure_lane_batches(1)
        self.assertEqual(set(limits.values()), {1})

    def test_rejects_cpu_oversubscription(self) -> None:
        with self.assertRaisesRegex(ValueError, "safe maximum|oversubscribes"):
            validate_worker_capacity(
                lane="ingestion",
                concurrency=4,
                per_tenant=2,
                db_pool_max=5,
                cpu_count=2,
                ocr_workers=2,
                pdf_processes=2,
            )

    def test_checkpoint_interface_is_safe_outside_a_worker(self) -> None:
        self.assertFalse(stage_checkpoint("extract", "running"))
        with processing_stage("extract") as metadata:
            metadata["pages"] = 2

    def test_worker_db_admission_context_reaches_fanout_threads(self) -> None:
        self.assertFalse(_WORKER_TASK_ACTIVE.get())
        with worker_db_task(), pool(2) as executor:
            inherited = list(executor.map(lambda _: _WORKER_TASK_ACTIVE.get(), range(2)))
        self.assertEqual(inherited, [True, True])
        self.assertFalse(_WORKER_TASK_ACTIVE.get())


if __name__ == "__main__":
    unittest.main()
