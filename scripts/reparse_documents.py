"""Re-parse a tenant's current PDF versions under the current parser.

Run after `PARSER_VERSION` changes (2026-10-07: PyMuPDF -> pypdfium2). Prints
counts and version-id prefixes only; no document text, no file names.

Usage:
    python scripts/reparse_documents.py --tenant <tenant uuid> [--deal <deal id>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main(argv: list[str] | None = None) -> int:
    from creditpaper.processing.reparse import current_pdf_versions, reparse_version

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--deal", default=None)
    args = ap.parse_args(argv)

    versions = current_pdf_versions(args.tenant, args.deal)
    failed = 0
    for deal_id, version_id, storage_path in versions:
        if not reparse_version(args.tenant, deal_id, version_id, storage_path=storage_path):
            failed += 1
            print(f"  failed: {version_id[:8]}")
    print(f"re-parsed {len(versions) - failed} of {len(versions)} PDF versions")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
