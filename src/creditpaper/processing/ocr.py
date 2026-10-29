"""Tesseract wrapper. Local OCR; no API spend.

OCR is not optional on this corpus: measured 2026-09-25, 24% of fixture pages
have no text layer at all. A machine without a working Tesseract reads roughly
a quarter of a deal as blank, and the failure is quiet — the pipeline catches
`TesseractNotFoundError` per page, leaves the text empty, and a document that
could not be read looks exactly like a document that states nothing.

FINDING THE BINARY. `pytesseract` shells out to `tesseract` and only looks on
PATH. The Windows installer does not put it there, so a developer who has
installed Tesseract still gets `TesseractNotFoundError` — which is what
happened here on 2026-09-25, with 5.4.0 sitting in Program Files the whole
time. So this looks in three places, in order:

  1. `TESSERACT_CMD`, for a deployment that puts it somewhere of its own;
  2. PATH, which is Linux, macOS and CI;
  3. the standard Windows install locations.

Resolution is cached: it is a filesystem probe, and it runs per page otherwise.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytesseract
from PIL import Image

from creditpaper.concurrency import ContextPreservingExecutor, pool
from creditpaper.obs.timing import timed

log = logging.getLogger(__name__)

# Where the Windows installers put it. Checked only after PATH, so a deliberate
# install elsewhere always wins.
_WINDOWS_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


@functools.cache
def tesseract_path() -> str | None:
    """The binary to run, or None when there is none to find."""
    configured = os.getenv("TESSERACT_CMD", "").strip()
    if configured:
        if Path(configured).is_file():
            return configured
        # A configured path that does not exist is a mistake worth saying out
        # loud, rather than quietly falling through to a different binary.
        log.warning("TESSERACT_CMD is set to a path that does not exist; ignoring it")
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in _WINDOWS_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def ocr_available() -> bool:
    """Whether OCR can run at all. For a startup check, not a per-page one."""
    from creditpaper.processing import ocr_rapid

    return tesseract_path() is not None or ocr_rapid.available()


# Tesseract spawns its own OpenMP threads per instance. Several pages are read
# at once here, so those threads oversubscribe the machine and every instance
# gets slower. Measured 2026-09-25, first on 12 real scanned pages read one at
# a time:
#
#     default threads     1000 ms/page   18,927 characters
#     OMP_THREAD_LIMIT=1   736 ms/page   18,927 characters
#
# then again as a within-run A/B over 40 real documents (298 OCR'd pages),
# because a saving measured serially is not evidence about a parallel run:
#
#     default threads     148.8s
#     OMP_THREAD_LIMIT=1  111.0s
#     OMP_THREAD_LIMIT=2  114.1s
#
# 25% for BYTE-IDENTICAL output, so there is nothing to trade off. Set before
# the first call rather than per call: the child process reads it at start-up.
#
# The two arms have to run in ONE process against one cleared cache. Comparing
# across separate runs first gave 207s against a 118s baseline and made this
# change look like a 75% regression — the machine was busy with something else,
# and the wall clock was measuring that.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")


# PAGE PREPARATION (spec §3.2). A scan arrives grey, unevenly lit and a few
# degrees off level, and Tesseract reads all three worse: a skewed line splits
# across two detected lines. So the page is levelled and contrast-stretched
# first, in PIL alone (binarising is left to Tesseract; see `prepare`).

# Skew is searched within this many degrees either way. Beyond it the page is
# turned, not skewed, and correcting a 90-degree page by 5 would only make it worse.
_MAX_SKEW = 5.0
_SKEW_STEP = 0.5
# The skew search runs on a page this tall, which is plenty to find text lines.
_SKEW_HEIGHT = 600


def binarise(img: Image.Image) -> Image.Image:
    """Two levels, black on white, at Otsu's threshold for this page."""
    grey = img.convert("L")
    hist = grey.histogram()
    total = sum(hist)
    weighted_total = sum(i * h for i, h in enumerate(hist))
    best_t, best_var = 128, -1.0
    count_b = weight_b = 0.0
    for t in range(256):
        count_b += hist[t]
        if count_b == 0:
            continue
        count_f = total - count_b
        if count_f == 0:
            break
        weight_b += t * hist[t]
        mean_b = weight_b / count_b
        mean_f = (weighted_total - weight_b) / count_f
        between = count_b * count_f * (mean_b - mean_f) ** 2
        if between > best_var:
            best_var, best_t = between, t
    return grey.point(lambda p: 255 if p > best_t else 0)


def _row_profile_variance(img: Image.Image) -> float:
    """Variance of the per-row ink share: highest when text lines are level."""
    ink = img.point(lambda p: 255 if p < 128 else 0)
    rows = list(ink.resize((1, ink.height), Image.Resampling.BOX).get_flattened_data())
    mean = sum(rows) / len(rows)
    return sum((r - mean) ** 2 for r in rows) / len(rows)


def estimate_skew(img: Image.Image) -> float:
    """The rotation (degrees, PIL's counter-clockwise convention) that levels the page's text lines."""
    grey = img.convert("L")
    scale = _SKEW_HEIGHT / grey.height if grey.height > _SKEW_HEIGHT else 1.0
    small = binarise(grey.resize((max(1, int(grey.width * scale)), max(1, int(grey.height * scale)))))

    def score(angle: float) -> tuple[float, float, float]:
        return (_row_profile_variance(small.rotate(angle, fillcolor=255)), -abs(angle), angle)

    # Probe centre and both directions first. Most pages are already level and
    # now need only five renders (the old exhaustive search needed 21). If one
    # direction wins, search only that half before the half-degree refinement.
    probes = [-2.0, 0.0, 2.0]
    best_probe = max(score(angle) for angle in probes)[2]
    if best_probe == 0.0:
        return max(score(angle) for angle in (-_SKEW_STEP, 0.0, _SKEW_STEP))[2]
    direction = 1 if best_probe > 0 else -1
    whole = [float(direction * angle) for angle in range(1, int(_MAX_SKEW) + 1)]
    best_whole = max(score(angle) for angle in whole)[2]
    nearby = [
        angle
        for angle in (best_whole - _SKEW_STEP, best_whole, best_whole + _SKEW_STEP)
        if -_MAX_SKEW <= angle <= _MAX_SKEW
    ]
    return max(score(angle) for angle in nearby)[2]


def prepare(img: Image.Image) -> Image.Image:
    """Greyscale, contrast-stretched and levelled: the page Tesseract reads.

    NOT binarised, though spec 3.2 asked for it: measured 2026-10-08, an explicit
    Otsu pass dropped a letter from small anti-aliased text ("Hello OCR" read as
    "HeloocR"; untouched, "Helloocr"), and Tesseract binarises internally anyway.
    Binarisation is used only to find the skew."""
    from PIL import ImageOps

    grey = ImageOps.autocontrast(img.convert("L"), cutoff=1)
    if os.getenv("OCR_DESKEW", "false").strip().lower() in {"0", "false", "no", "off"}:
        return grey
    angle = estimate_skew(grey)
    if angle:
        grey = grey.rotate(angle, expand=True, fillcolor=255, resample=Image.Resampling.BICUBIC)
    return grey


@timed("ocr")
def ocr_page(img: Image.Image) -> str:
    """Run Tesseract on one levelled page (`prepare`). Returns concatenated text from the page.

    `--psm 6`, kept after measuring the alternative spec 3.2 suggested: on 40
    sampled blind_01 scans (2026-10-08) `--psm 3` with a `--psm 4` retry read 5%
    fewer words and far fewer on drawing sheets (7 words against 873 on one),
    while the levelled page with `--psm 6` read 99.7% of the old raw read's
    words in 13% less time."""
    resolved = tesseract_path()
    if resolved is None:
        # No Tesseract binary on this host (a pip-only deploy): the second engine,
        # which installs from pip alone, reads the page instead. The cross-check
        # (`vision_check.read_page_by_second_ocr`) is then off, since it would be
        # the same engine agreeing with itself.
        from creditpaper.processing import ocr_rapid

        if ocr_rapid.available():
            return ocr_rapid.read_image(img)
    if resolved:
        pytesseract.pytesseract.tesseract_cmd = resolved
    # A pathological page must not monopolize an ingestion lane. Pages that
    # exceed this budget are explicitly marked ocr_failed by the PDF extractor
    # and remain eligible for the slower review/vision path.
    timeout = float(os.getenv("OCR_TIMEOUT_S", "5"))
    return pytesseract.image_to_string(prepare(img), config="--oem 1 --psm 6", timeout=timeout)


# PAGE-LEVEL CONCURRENCY.
#
# Documents already parse in parallel, but the pages inside one document were
# read in a `for` loop, so a single 200-page scanned deed OCR'd on ONE core
# while the other seven sat idle. That is the shape of the tail: the run is as
# long as its worst document, not as long as its total work divided by cores.
#
# Tesseract is a separate PROCESS, so it neither holds the GIL nor benefits from
# more threads in ours — measured CPU/wall on a parse was 0.14, i.e. almost all
# of it was spent waiting on subprocesses. The bound that matters is cores.
#
# The pool is PROCESS-WIDE and created once, which is the whole point: eight
# document workers each starting their own eight-wide pool would put 64
# Tesseracts on 8 cores and each would get slower. Here the total number of
# Tesseracts running at any moment is `_ocr_workers()` no matter how many
# documents are in flight.
#
# No deadlock: this pool never submits back into the pool that calls it.
_OCR_POOL_ENV = "OCR_WORKERS"


def _ocr_workers() -> int:
    # OCR runs inside the document worker pool.  A CPU-sized pool per document
    # multiplies rapidly (4 documents x 16 cores = 64 threads), causing heavy
    # context switching and connection starvation.  Keep the default bounded;
    # operators can raise it explicitly with OCR_WORKERS on larger hosts.
    configured = int(os.getenv(_OCR_POOL_ENV, "0"))
    return configured if configured > 0 else min(4, os.cpu_count() or 1)


@functools.cache
def _ocr_pool() -> ContextPreservingExecutor:
    return pool(_ocr_workers(), thread_name_prefix="ocr")


def ocr_pages(images: Sequence[Image.Image]) -> list[str | None]:
    """OCR page images concurrently. `None` where a page could not be read.

    A failure is per page and never raises: one unreadable page must not lose
    the rest of a document. The caller keeps whatever text it already had and
    leaves `ocr_used` false, so a page that failed is not mistaken for a page
    that was read and found to say nothing.
    """
    if not images:
        return []
    if len(images) == 1:
        return [_read(images[0])]
    return list(_ocr_pool().map(_read, images))


def _read(img: Image.Image) -> str | None:
    try:
        return ocr_page(img)
    except Exception as exc:
        log.warning("OCR failed on one page: %s", type(exc).__name__)
        return None
