"""Dependency-light checks for bounded document batch registration."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from creditpaper.deals.services import workspace


class _Cursor:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.calls = 0
        self.outcomes: list[dict] = []
        self.duplicate = duplicate

    def execute(self, _query: str, params: tuple) -> None:
        self.calls += 1
        uploads = json.loads(params[3])
        self.outcomes = []
        for item in uploads:
            if self.duplicate:
                self.outcomes.append(
                    {
                        "client_key": item["client_key"],
                        "status": "duplicate",
                        "display_name": "existing.pdf",
                        "version_number": 1,
                    }
                )
            else:
                self.outcomes.append(
                    {
                        "client_key": item["client_key"],
                        "status": "ok",
                        "job_id": str(uuid4()),
                        "version_number": 1,
                        "document": {
                            "id": item["document_id"],
                            "display_name": item["display_name"],
                            "document_kind": None,
                            "created_at": item["uploaded_at"],
                        },
                    }
                )

    def fetchone(self) -> tuple[list[dict]]:
        return (self.outcomes,)


class BatchRegistrationTests(unittest.TestCase):
    def _run(self, count: int, *, duplicate: bool = False) -> tuple[list[dict], _Cursor, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        cursor = _Cursor(duplicate=duplicate)

        @contextmanager
        def tenant_cursor(_tenant: str):
            yield cursor

        def write(path: Path, _data: bytes, **_kwargs) -> None:
            path.write_bytes(b"encrypted")

        uploads = [
            {
                "client_key": f"folder/{index}.pdf",
                "file_bytes": f"document-{index}".encode(),
                "original_filename": f"{index}.pdf",
                "mime_type": "application/pdf",
                "display_name": f"folder/{index}.pdf",
            }
            for index in range(count)
        ]
        with (
            patch.object(workspace, "STORAGE_ROOT", root),
            patch("creditpaper.db.tenant_cursor", tenant_cursor),
            patch("creditpaper.security.document_storage.write_document", write),
        ):
            results = workspace.upload_document_batch(
                tenant_id=str(uuid4()),
                deal_id="deal_batch_test",
                actor_id=str(uuid4()),
                uploads=uploads,
            )
        return results, cursor, root

    def test_twenty_five_documents_use_one_database_call(self) -> None:
        results, cursor, root = self._run(25)
        self.assertEqual(cursor.calls, 1)
        self.assertEqual(len(results), 25)
        self.assertTrue(all(item["status"] == "ok" for item in results))
        self.assertEqual(len(list(root.rglob("*.pdf"))), 25)

    def test_duplicate_outcome_removes_unreferenced_ciphertext(self) -> None:
        results, cursor, root = self._run(1, duplicate=True)
        self.assertEqual(cursor.calls, 1)
        self.assertEqual(results[0]["status"], "duplicate")
        self.assertEqual(list(root.rglob("*.pdf")), [])

    def test_batch_size_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "1 to 50"):
            workspace.upload_document_batch(
                tenant_id=str(uuid4()), deal_id="deal", actor_id=str(uuid4()), uploads=[]
            )


if __name__ == "__main__":
    unittest.main()
