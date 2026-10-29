"""Re-parse stored document versions under the current parser (spec §3.4).

Bumping `ingest.page_text.PARSER_VERSION` makes every parse row from the old
parser a cache miss, but it does not re-parse anything: `parse_cache.put` copies
the page text already stored in `deal_document_extractions`, which the OLD
parser wrote. Until a version is re-parsed, the window path raises `LookupError`
on it. This re-runs extraction on a version and registers the new parse.
Run per tenant with `scripts/reparse_documents.py` after a parser change.
"""

from __future__ import annotations

import logging
from pathlib import Path

from creditpaper.db import tenant_cursor
from creditpaper.ingest import parse_cache
from creditpaper.ingest.page_text import pages_for_version
from creditpaper.processing.pipeline import process_version
from creditpaper.processing.store import update_status

log = logging.getLogger(__name__)

__all__ = ["current_pdf_versions", "reparse_version"]


def reparse_version(tenant_id: str, deal_id: str, version_id: str, *, storage_path: str) -> bool:
    """Re-extract one version and register its parse under the current parser; False when it failed."""
    raw = Path(storage_path).read_bytes()
    # `process_version` treats a ready version as done, so it is reset first.
    update_status(version_id, "pending", tenant_id=tenant_id, deal_id=deal_id)
    if not process_version(version_id, tenant_id=tenant_id, deal_id=deal_id):
        log.warning("re-parse failed for version %s", version_id[:8])
        return False
    pages = pages_for_version(tenant_id, version_id)
    if pages is None:
        return False
    parse_cache.put(
        tenant_id,
        parse_cache.content_hash(raw),
        version_id=version_id,
        route=pages.route,
        page_count=pages.page_count,
    )
    return True


def current_pdf_versions(tenant_id: str, deal_id: str | None = None) -> list[tuple[str, str, str]]:
    """``(deal_id, version_id, storage_path)`` of every current PDF version of the tenant (or one deal)."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """SELECT v.deal_id::text, v.id::text, v.storage_path FROM deal_document_versions v
            WHERE v.tenant_id = %s AND v.superseded_at IS NULL AND v.mime_type = 'application/pdf'
            AND (%s::text IS NULL OR v.deal_id::text = %s::text) ORDER BY v.deal_id, v.id""",
            (tenant_id, deal_id, deal_id),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]
