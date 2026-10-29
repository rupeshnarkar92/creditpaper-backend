"""Offline safety checks; run with unittest (no application DB fixtures)."""

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from scripts import benchmark_slot_matching as benchmark


class BenchmarkSafetyTests(unittest.TestCase):
    def test_main_covers_entire_probe_with_rollback_boundary(self):
        events = []

        @contextmanager
        def boundary():
            events.append("enter")
            yield
            events.append("exit")

        with patch.object(benchmark, "rollback_database", boundary):
            with patch.object(benchmark, "run_probe", side_effect=lambda: events.append("probe")):
                benchmark.main()
        self.assertEqual(events, ["enter", "probe", "exit"])

    def test_every_checkout_rolls_back_including_lazy_board_loads(self):
        events = []

        class Connection:
            @contextmanager
            def transaction(self, *, force_rollback=False):
                events.append(("begin", force_rollback))
                try:
                    yield
                finally:
                    events.append("rollback" if force_rollback else "commit")

        class Pool:
            @contextmanager
            def connection(self, *args, **kwargs):
                yield Connection()

        with patch.object(benchmark.db, "get_pool", return_value=Pool()):
            with benchmark.rollback_database():
                # Both initial lazy creation and later writes use this boundary.
                for _ in range(2):
                    with benchmark.db.get_pool().connection():
                        events.append("write")
            self.assertIsInstance(benchmark.db.get_pool(), Pool)
        self.assertEqual(events, [("begin", True), "write", "rollback"] * 2)

    def test_exception_restores_pool_and_rolls_back(self):
        events = []

        class Connection:
            @contextmanager
            def transaction(self, *, force_rollback=False):
                try:
                    yield
                finally:
                    events.append(force_rollback)

        class Pool:
            @contextmanager
            def connection(self):
                yield Connection()

        pool = Pool()
        with patch.object(benchmark.db, "get_pool", return_value=pool):
            with self.assertRaisesRegex(RuntimeError, "probe"):
                with benchmark.rollback_database():
                    with benchmark.db.get_pool().connection():
                        raise RuntimeError("probe")
            self.assertIs(benchmark.db.get_pool(), pool)
        self.assertEqual(events, [True])
