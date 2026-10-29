"""Offline transaction-boundary regression tests for document persistence."""

import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from creditpaper.processing.store import upsert_extraction


class FinalizeTests(unittest.TestCase):
    def test_finalization_uses_one_transaction_and_fences_before_writes(self):
        events = []
        cur = Mock()
        cur.execute.side_effect = lambda sql, params: events.append(sql)
        cur.fetchone.return_value = (True,)

        @contextmanager
        def transaction(tenant):
            events.append("begin")
            yield cur
            events.append("commit")

        with (
            patch("creditpaper.processing.store.tenant_cursor", transaction),
            patch("creditpaper.ledger.jobs.fence", side_effect=lambda c: events.append("fence")),
            patch("creditpaper.ledger.retrieval.index_pages", side_effect=lambda *a: events.append("index")),
        ):
            upsert_extraction(
                version_id="version",
                tenant_id="tenant",
                deal_id="deal",
                document_id="document",
                version_number=1,
                mime_type="application/pdf",
                full_text="text",
                page_texts=[],
                chunks=[],
                page_count=1,
                sheet_count=None,
                char_count=4,
                classification={"kind": "valuation"},
                finalize=True,
            )
        self.assertEqual(events[0:2], ["begin", "fence"])
        self.assertEqual(events[-1], "commit")
        self.assertEqual(events.count("begin"), 1)
        self.assertEqual(len(events), 4)
        self.assertIn("finish_document_processing_cached", events[-2])


if __name__ == "__main__":
    unittest.main()
