"""Per-page OCR bench: compare readers on scanned pages without a full extraction run (spec §3.7).

Each case is one scanned page and the strings a correct read must contain — a
figure, a party, a date. A reader passes a case when every string is in its text
(whitespace- and case-insensitive). Binary, per page, so two OCR options can be
compared in minutes instead of a costed extraction run.

CASES ARE TENANT DATA. They live in `fixtures/ocr_bench/*.json` (gitignored), one
file per document:

    {"file": "blind/blind_01/deal_files/<name>.pdf", "pages": {"7": ["12,400,000", "4 March 2027"]}}

`file` is relative to `fixtures/`. This prints counts and page numbers only:
no document text, no file names, no expected strings.

Readers: `tesseract-psm6` (the pre-2026-10-07 reader: raw page, one text block),
`tesseract` (the current reader: prepared page, psm 3 then 4), and `vision`
(Claude transcription; spends, so only with --vision and a --budget-usd).

Usage:
    python scripts/ocr_page_bench.py
    python scripts/ocr_page_bench.py --vision --budget-usd 2
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

VISION_USD_PER_PAGE_EST = 0.03
DPI = 200


@dataclass(frozen=True)
class Case:
    document: str  # an anonymous label, never the file name
    path: Path
    page: int  # 1-based
    must_contain: tuple[str, ...]


Reader = Callable[[object], str]  # a rendered page image -> its text


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def passes(text: str, must_contain: Sequence[str]) -> bool:
    body = _norm(text)
    return all(_norm(s) in body for s in must_contain)


def load_cases(folder: Path, fixtures: Path) -> list[Case]:
    cases: list[Case] = []
    for i, spec_file in enumerate(sorted(folder.glob("*.json")), start=1):
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        path = fixtures / spec["file"]
        for page, strings in sorted(spec["pages"].items(), key=lambda kv: int(kv[0])):
            cases.append(Case(f"doc{i}", path, int(page), tuple(strings)))
    return cases


def run(
    cases: Sequence[Case], readers: Mapping[str, Reader], render: Callable[[Path, int], object]
) -> dict[str, list[bool]]:
    """Per reader, one pass/fail per case, in case order."""
    results: dict[str, list[bool]] = {name: [] for name in readers}
    for case in cases:
        image = render(case.path, case.page)
        for name, reader in readers.items():
            try:
                text = reader(image)
            except Exception:  # an engine failure is a failed read, not a crashed bench
                text = ""
            results[name].append(passes(text, case.must_contain))
    return results


def _render(path: Path, page: int) -> object:
    from creditpaper.ingest.reader import render_pages

    (image,) = render_pages(path, [page - 1], dpi=DPI)
    if image is None:
        raise SystemExit(f"page {page} of a case document could not be rendered")
    return image


def _readers(vision: bool) -> dict[str, Reader]:
    import pytesseract

    from creditpaper.processing import ocr

    def legacy(image: object) -> str:
        path = ocr.tesseract_path()
        if path:
            pytesseract.pytesseract.tesseract_cmd = path
        return pytesseract.image_to_string(image, config="--oem 1 --psm 6")

    readers: dict[str, Reader] = {"tesseract-psm6": legacy, "tesseract": ocr.ocr_page}
    if vision:
        import base64
        import io

        from creditpaper.ingest.transcribe import transcribe_page

        def by_vision(image: object) -> str:
            buf = io.BytesIO()
            image.save(buf, format="PNG")  # type: ignore[attr-defined]
            return transcribe_page(base64.standard_b64encode(buf.getvalue()).decode("ascii"))

        readers["vision"] = by_vision
    return readers


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixtures", type=Path, default=ROOT / "fixtures")
    ap.add_argument("--vision", action="store_true", help="also read each page by vision (spends)")
    ap.add_argument("--budget-usd", type=float, default=0.0, help="refuse a vision run estimated above this")
    args = ap.parse_args(argv)

    cases = load_cases(args.fixtures / "ocr_bench", args.fixtures)
    if not cases:
        print("no cases in fixtures/ocr_bench/ (see the docstring for the format)")
        return 2
    if args.vision:
        estimate = len(cases) * VISION_USD_PER_PAGE_EST
        if estimate > args.budget_usd:
            print(f"vision estimate ${estimate:.2f} exceeds --budget-usd ${args.budget_usd:.2f}")
            return 2
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")

    results = run(cases, _readers(args.vision), _render)
    print(f"{len(cases)} pages")
    for name, outcomes in results.items():
        failed = [f"{c.document}:p{c.page}" for c, ok in zip(cases, outcomes, strict=True) if not ok]
        print(
            f"  {name:<16} {sum(outcomes):>4} / {len(outcomes)}"
            + (f"   failed {', '.join(failed)}" if failed else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
