"""Dependency-light checks for streaming encrypted document storage."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creditpaper.security.document_storage import (
    STREAM_MAGIC,
    materialize_document,
    read_document,
    write_document_from_path,
)


class StreamingDocumentStorageTests(unittest.TestCase):
    def test_large_file_round_trip_is_streamed_and_bound_to_aad(self) -> None:
        key = b"k" * 32
        payload = os.urandom(3 * 1024 * 1024 + 17)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("creditpaper.security.document_storage._envelope_key", return_value=key),
        ):
            root = Path(directory)
            source, encrypted = root / "source.zip", root / "stored.enc"
            tenant_a = "00000000-0000-0000-0000-000000000001"
            tenant_b = "00000000-0000-0000-0000-000000000002"
            batch = "00000000-0000-0000-0000-000000000003"
            source.write_bytes(payload)
            write_document_from_path(encrypted, source, tenant_id=tenant_a, record_id=batch)
            self.assertTrue(encrypted.read_bytes().startswith(STREAM_MAGIC))
            self.assertEqual(read_document(encrypted, tenant_id=tenant_a, record_id=batch), payload)
            with materialize_document(
                encrypted, tenant_id=tenant_a, record_id=batch, suffix=".zip"
            ) as materialized:
                self.assertEqual(materialized.read_bytes(), payload)
            with self.assertRaises(ValueError):
                read_document(encrypted, tenant_id=tenant_b, record_id=batch)


if __name__ == "__main__":
    unittest.main()
