"""A second, independent local OCR engine: PP-OCR models through onnxruntime (RapidOCR).

WHY A SECOND ENGINE. A fact read off a scanned page is capped for review, because
one OCR read is the weakest text the pipeline holds (`verify.Reason.OCR_TEXT`).
The cap used to be lifted only by a Claude vision read, which costs money and
seconds per page. A second engine with a different architecture (Tesseract is an
LSTM line reader; PP-OCR is a detector plus a CTC recogniser, trained on other
data) misreads different glyphs, so two engines stating the same figure is strong
evidence the figure is what the page says, at no API cost. Claude vision is kept
for the facts the two engines dispute (`ingest/vision_check.confirm_in_tiers`).

DEPLOYABLE AS A PLAIN WHEEL. Both the runtime (onnxruntime) and the models (bundled
in the rapidocr wheel) install from pip: no system binary, no download at first
use, nothing leaves the machine. Apache-2.0 code and models.

Absent the package, `available()` is False and the cascade skips this tier: the
facts stay capped exactly as before, never wrongly confirmed.
"""

from __future__ import annotations

import functools
import logging
import threading
from typing import Any

from PIL import Image

from creditpaper.obs.timing import timed

log = logging.getLogger(__name__)

__all__ = ["available", "read_image"]

# One engine per thread: the cascade reads pages on a pool, and an onnxruntime
# session pinned to one thread per call keeps N parallel pages from fighting over
# the same cores (the same oversubscription Tesseract showed; see `ocr.py`).
_local = threading.local()


@functools.cache
def available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import rapidocr  # noqa: F401
    except Exception:
        return False
    return True


def _engine() -> Any:
    engine = getattr(_local, "engine", None)
    if engine is None:
        from rapidocr import RapidOCR

        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        engine = RapidOCR(
            params={
                "Global.log_level": "warning",
                "EngineConfig.onnxruntime.intra_op_num_threads": 1,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            }
        )
        _local.engine = engine
    return engine


@timed("ocr_rapid")
def read_image(img: Image.Image) -> str:
    """The page's text, one detected line per output line, top to bottom."""
    result = _engine()(img.convert("RGB"))
    return "\n".join(result.txts or ())
