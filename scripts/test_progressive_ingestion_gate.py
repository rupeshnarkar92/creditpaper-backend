"""Regression tests for capability-specific progressive readiness."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi import HTTPException

from creditpaper.deals.services import ingestion_gate


class _Cursor:
    def __init__(self, status):
        self.status = status

    def execute(self, *_args):
        pass

    def fetchone(self):
        return (self.status,)


@contextmanager
def _tenant_cursor(status):
    yield _Cursor(status)


class ProgressiveGateTests(unittest.TestCase):
    def test_facts_unlock_after_first_ready_document(self):
        status = {"facts_locked": False, "generation_locked": True, "locked": True}
        with patch.object(ingestion_gate, "tenant_cursor", side_effect=lambda _tenant: _tenant_cursor(status)):
            self.assertIsNone(ingestion_gate.require_ready("tenant", "deal", capability="facts"))
            with self.assertRaises(HTTPException):
                ingestion_gate.require_ready("tenant", "deal", capability="generation")

    def test_unknown_capability_is_rejected(self):
        with self.assertRaises(ValueError):
            ingestion_gate.require_ready("tenant", "deal", capability="unknown")


if __name__ == "__main__":
    unittest.main()
