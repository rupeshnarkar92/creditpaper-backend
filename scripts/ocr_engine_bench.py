"""Tesseract vs RapidOCR (PP-OCR via onnxruntime) on fixture pages. Local, no API. Prints counts only.

A: born-digital pages rendered and degraded like a scan; truth = the text layer's numeric
   tokens. Reports each engine's recall, agreement precision, and whether agreed tokens
   missing from the text layer are one edit from a true token (a SHARED MISREAD, the
   failure that would make cross-engine confirmation unsafe) or far from all (content the
   text layer lacks, e.g. figures inside images).
B: real scanned pages (no text layer); agreement between the engines.

Usage: python scripts/ocr_engine_bench.py N_DIGITAL N_SCAN [--mild] [--no-b]
Run ONE at a time: two in parallel exhausted commit memory on 2026-10-09.
"""

from __future__ import annotations

import io
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageFilter

from creditpaper.ingest.reader import PdfiumReader, render_pages
from creditpaper.ingest.values import numeric_tokens
from creditpaper.processing.ocr import ocr_page, prepare

# Fixtures are gitignored tenant data; the integrate/jason worktree has none of its own.
FIX = Path(os.getenv("FIXTURES_DIR", r"C:\dev\creditpaper\fixtures"))
N_DIGITAL = int(sys.argv[1]) if len(sys.argv) > 1 else 50
N_SCAN = int(sys.argv[2]) if len(sys.argv) > 2 else 50
if "--no-b" in sys.argv:
    N_SCAN = 0
random.seed(7)

pdfs = sorted(p for p in FIX.rglob("*.pdf") if "deal_files" in p.parts or "build_deal" in p.parts)
digital, scanned = [], []
for pdf in pdfs:
    try:
        with PdfiumReader(pdf) as r:
            for n in range(1, r.page_count() + 1):
                text = r.page(n).text
                if len(text.strip()) < 30:
                    scanned.append((pdf, n))
                elif len(numeric_tokens(text)) >= 8:
                    digital.append((pdf, n))
    except Exception:
        continue
print(f"pdfs={len(pdfs)} digital_candidates={len(digital)} scanned_pages={len(scanned)}")
digital = random.sample(digital, min(N_DIGITAL, len(digital)))
scanned = random.sample(scanned, min(N_SCAN, len(scanned)))


MILD = "--mild" in sys.argv


def degrade(img: Image.Image) -> Image.Image:
    g = img.convert("L").rotate(random.uniform(-1.2, 1.2), expand=True, fillcolor=255)
    g = g.filter(ImageFilter.GaussianBlur(0.4 if MILD else 0.7))
    px = g.load()
    w, h = g.size
    for _ in range(w * h // (4000 if MILD else 400)):
        x, y = random.randrange(w), random.randrange(h)
        px[x, y] = random.choice((0, 90, 200))
    buf = io.BytesIO()
    g.save(buf, format="JPEG", quality=75 if MILD else 55)
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


import logging  # noqa: E402

from rapidocr import RapidOCR  # noqa: E402

logging.getLogger("RapidOCR").setLevel(logging.ERROR)
rapid = RapidOCR()


def rapid_text(img: Image.Image) -> str:
    r = rapid(img)
    return "\n".join(r.txts or ())


def rapid_prepared(img: Image.Image) -> str:
    return rapid_text(prepare(img).convert("RGB"))


ENGINES = {"tesseract": ocr_page, "rapid": rapid_text}


timing: Counter[str] = Counter()


def read_all(img):
    out = {}
    for name, fn in ENGINES.items():
        t = time.perf_counter()
        try:
            out[name] = set(numeric_tokens(fn(img)))
        except Exception as e:
            print("fail", name, type(e).__name__)
            out[name] = set()
        timing[name] += time.perf_counter() - t
    return out


# --- A: digital pages, degraded --------------------------------------------------
stats = Counter()
for pdf, n in digital:
    with PdfiumReader(pdf) as r:
        text = r.page(n).text
    truth = set(numeric_tokens(text))
    (img,) = render_pages(pdf, [n - 1], dpi=200)
    reads = read_all(degrade(img))
    stats["truth"] += len(truth)
    for name, got in reads.items():
        stats[f"{name}_hit"] += len(got & truth)
        stats[f"{name}_extra"] += len(got - truth)
    both = reads["tesseract"] & reads["rapid"]
    # Is each agreed token absent from the text layer a shared MISREAD (one edit from a
    # true token) or real content the text layer lacks? Also: does the page have images?
    from rapidfuzz.distance import Levenshtein

    for tok in both - truth:
        near = any(len(tok) == len(t) and Levenshtein.distance(tok, t) == 1 for t in truth)
        stats["agreed_false_near" if near else "agreed_false_far"] += 1
        stats[f"agreed_false_len{min(len(tok), 6)}"] += 1
    either = reads["tesseract"] | reads["rapid"]
    stats["agree"] += len(both)
    stats["agree_true"] += len(both & truth)
    stats["either_hit"] += len(either & truth)
    stats["only_tess_true"] += len((reads["tesseract"] - reads["rapid"]) & truth)
    stats["only_rapid_true"] += len((reads["rapid"] - reads["tesseract"]) & truth)
T = stats["truth"]
print(f"\nA: {len(digital)} degraded digital pages, {T} true numeric tokens")
for name in ENGINES:
    print(
        f"  {name:11s} recall {stats[f'{name}_hit'] / T:.3f}  extra(not in truth) {stats[f'{name}_extra']}"
        f"  {timing[name] / len(digital):.2f}s/page"
    )
print(f"  either-engine recall {stats['either_hit'] / T:.3f}")
print(
    f"  agreed tokens {stats['agree']}, of which true {stats['agree_true']} "
    f"(precision {stats['agree_true'] / max(1, stats['agree']):.4f}), agreed recall {stats['agree_true'] / T:.3f}"
)
print(
    f"  true tokens only tesseract read {stats['only_tess_true']}, only rapid read {stats['only_rapid_true']}"
)

print(
    f"  agreed-not-in-text-layer: one edit from a true token {stats['agreed_false_near']}, "
    f"far {stats['agreed_false_far']}; by length " + str({k: v for k, v in stats.items() if "len" in k})
)
# --- B: real scans ----------------------------------------------------------------
timing.clear()
b = Counter()
for pdf, n in scanned:
    (img,) = render_pages(pdf, [n - 1], dpi=200)
    reads = read_all(img)
    for name, got in reads.items():
        b[name] += len(got)
    b["agree"] += len(reads["tesseract"] & reads["rapid"])
    b["union"] += len(reads["tesseract"] | reads["rapid"])
print(f"\nB: {len(scanned)} real scanned pages")
for name in ENGINES:
    print(f"  {name:11s} tokens {b[name]}  {timing[name] / max(1, len(scanned)):.2f}s/page")
print(f"  agreed {b['agree']} of union {b['union']} ({b['agree'] / max(1, b['union']):.3f})")
