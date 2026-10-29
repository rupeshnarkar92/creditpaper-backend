"""Fast, dependency-light checks for ingestion safety invariants.

This complements the corpus benchmark: it protects decisions that are easy to
regress but do not require tenant documents or paid model calls.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from creditpaper.processing import ocr as ocr_module
from creditpaper.processing.extractors import pdf as pdf_module
from creditpaper.processing.extractors.pdf import _needs_ocr
from creditpaper.processing.models import PageText
from creditpaper.processing.ocr import estimate_skew
from creditpaper.processing.store import PARSER_VERSION
from creditpaper.processing.vision import machine_read


class IngestionSafetyTests(unittest.TestCase):
    def test_blank_page_does_not_consume_ocr_budget(self) -> None:
        self.assertFalse(_needs_ocr("", has_image=False, has_visual_content=False))

    def test_scanned_page_is_sent_to_ocr(self) -> None:
        self.assertTrue(_needs_ocr("", has_image=True, has_visual_content=True))

    def test_short_born_digital_page_keeps_its_text_layer(self) -> None:
        self.assertFalse(_needs_ocr("Signed", has_image=False, has_visual_content=True))

    def test_short_hybrid_page_is_sent_to_ocr(self) -> None:
        self.assertTrue(_needs_ocr("Signed", has_image=True, has_visual_content=True))

    def test_failed_ocr_remains_machine_read(self) -> None:
        page = PageText(page=7, text="", ocr_attempted=True, ocr_failed=True)
        self.assertEqual(machine_read([page.model_dump()]), frozenset({7}))

    def test_deferred_ocr_remains_machine_read(self) -> None:
        page = PageText(page=7, text="", ocr_deferred=True)
        self.assertEqual(machine_read([page.model_dump()]), frozenset({7}))

    def test_confirmed_second_read_clears_machine_read_flag(self) -> None:
        page = PageText(page=7, text="value", ocr_attempted=True, ocr_used=True)
        payload = page.model_dump() | {"ocr_agrees": True}
        self.assertEqual(machine_read([payload]), frozenset())

    def test_extraction_cache_tracks_reader_version(self) -> None:
        from creditpaper.ingest.reader import READER_VERSION

        self.assertIn(READER_VERSION, PARSER_VERSION)

    def test_fast_skew_search_finds_half_degree_rotation(self) -> None:
        page = Image.new("L", (600, 500), 255)
        draw = ImageDraw.Draw(page)
        for y in range(30, 470, 30):
            draw.text((30, y), "Facility limit 12,400,000", fill=0)
        self.assertEqual(estimate_skew(page.rotate(-2.5, fillcolor=255)), 2.5)

    def test_missing_tesseract_uses_packaged_local_fallback(self) -> None:
        page = Image.new("L", (20, 20), 255)
        with (
            patch.object(ocr_module, "tesseract_path", return_value=None),
            patch("creditpaper.processing.ocr_rapid.available", return_value=True),
            patch("creditpaper.processing.ocr_rapid.read_image", return_value="Recovered text"),
        ):
            self.assertEqual(ocr_module.ocr_page(page), "Recovered text")

    def test_pdf_timeout_cancels_job_and_recycles_children(self) -> None:
        class Future:
            cancelled = False

            def result(self, *, timeout: float):
                self.timeout = timeout
                raise TimeoutError

            def cancel(self) -> None:
                self.cancelled = True

        class Pool:
            terminated = False

            def __init__(self) -> None:
                self.future = Future()

            def submit(self, *_args):
                return self.future

            def terminate_workers(self) -> None:
                self.terminated = True

        pool = Pool()
        with (
            patch.object(pdf_module, "_pdf_pool", return_value=pool) as pool_factory,
            patch.dict("os.environ", {"PDF_EXTRACT_PROCESSES": "1", "PDF_EXTRACT_TIMEOUT_S": "3"}),
        ):
            with self.assertRaisesRegex(TimeoutError, "exceeded 3 seconds"):
                pdf_module.extract_pdf(b"bad pdf", original_filename="bad.pdf")
        self.assertTrue(pool.future.cancelled)
        self.assertTrue(pool.terminated)
        pool_factory.cache_clear.assert_called_once()


if __name__ == "__main__":
    unittest.main()
