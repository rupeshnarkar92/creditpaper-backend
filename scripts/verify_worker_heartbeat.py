"""Offline regression checks for process-level lease renewal."""

import json
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from creditpaper.ledger import jobs


class BatchedHeartbeatTests(unittest.TestCase):
    def tearDown(self) -> None:
        with jobs._ACTIVE_LEASES_LOCK:
            jobs._ACTIVE_LEASES.clear()

    def test_one_database_call_renews_all_leases_for_a_tenant(self) -> None:
        calls: list[tuple[str, tuple]] = []
        cursor = Mock()
        cursor.execute.side_effect = lambda sql, params: calls.append((sql, params))

        @contextmanager
        def transaction(_tenant_id):
            yield cursor

        with jobs._ACTIVE_LEASES_LOCK:
            jobs._ACTIVE_LEASES.update(
                {
                    "00000000-0000-0000-0000-000000000001": (
                        "10000000-0000-0000-0000-000000000001",
                        "20000000-0000-0000-0000-000000000001",
                    ),
                    "00000000-0000-0000-0000-000000000002": (
                        "10000000-0000-0000-0000-000000000001",
                        "20000000-0000-0000-0000-000000000002",
                    ),
                }
            )

        with patch("creditpaper.ledger.jobs.tenant_cursor", transaction):
            jobs._renew_active_leases()

        self.assertEqual(len(calls), 1)
        self.assertIn("refresh_worker_leases", calls[0][0])
        leases = json.loads(calls[0][1][1])
        self.assertEqual({item["id"] for item in leases}, set(jobs._ACTIVE_LEASES))


if __name__ == "__main__":
    unittest.main()
