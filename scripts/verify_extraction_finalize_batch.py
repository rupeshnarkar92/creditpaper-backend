"""Offline regression check for tenant-scoped extraction micro-batching."""

import threading
import unittest
from unittest.mock import patch

from creditpaper.processing import finalize_batch


class FinalizeBatchTests(unittest.TestCase):
    def test_concurrent_documents_share_one_database_batch(self) -> None:
        written: list[tuple[str, list[dict]]] = []
        tenant = "10000000-0000-0000-0000-000000000001"

        def write(tenant_id, items):
            written.append((tenant_id, items))

        with patch("creditpaper.processing.finalize_batch._write_batch", side_effect=write):
            threads = [
                threading.Thread(
                    target=finalize_batch.finalize_document,
                    args=(tenant, {"version_id": str(index)}),
                )
                for index in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0][0], tenant)
        self.assertEqual(len(written[0][1]), 4)


if __name__ == "__main__":
    unittest.main()
