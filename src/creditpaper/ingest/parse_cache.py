"""Parse once per distinct byte sequence, per parser version, ever.

Parsing is cheap (46 pages/s) but not free, and OCR is not cheap at all. Caching
against the content hash means a standard form that appears on twenty deals is
read once, a re-upload of the same file is read zero times, and a worker restart
resumes instead of re-running. `parser_version` is part of the key: a parser
upgrade must not read stale output back as if it were the current parser's work,
so it gets its own row rather than overwriting or being blocked by the old one.

Tenant-scoped by row and by RLS: the same hash in two tenants is two rows,
because a cache hit across tenants would leak the fact that another firm holds
the same document.

`put` is insert-if-absent, never upsert (`ON CONFLICT ... DO NOTHING`): only a
complete, successful parse may be written, and once written for a given
(tenant, hash, parser_version) it is final. A partial parse — a crash mid-document,
a timeout — must never reach `put`, or a later, complete read of the same bytes
would never run because the cache would already claim to hold it.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, model_validator

from creditpaper.db import tenant_cursor
from creditpaper.ingest.page_text import PARSER_VERSION, pages_for_version
from creditpaper.ingest.probe import Route

# A parser_version starting with this marks a row of vision transcriptions
# (ingest/transcribe.py): text a model read off the page image, not the page's
# own text layer. Verification treats it as a read to be confirmed, not proof.
VISION_PREFIX = "vision-"


class CachedParse(BaseModel):
    hash: str
    route: Route
    page_count: int
    # Defaulted, and `version_id` kept, so both ingestion lines construct this
    # the same way while the merged model is measured (2026-09-23). The
    # feat/ingestion line added `parser_version` and dropped `version_id`; the
    # devel_jason line did the reverse, and its callers pass a version id.
    parser_version: str = PARSER_VERSION
    version_id: str | None = None
    pages: dict[int, str]

    @property
    def vision_sourced(self) -> bool:
        return self.parser_version.startswith(VISION_PREFIX)

    @model_validator(mode="after")
    def _pages_within_page_count(self) -> CachedParse:
        """Missing keys just mean a page was never read; out-of-range keys are invalid."""
        for page in self.pages:
            if not (1 <= page <= self.page_count):
                raise ValueError(f"page key {page} is outside 1..{self.page_count}")
        return self


def content_hash(data: bytes) -> str:
    """Content-address the raw bytes: stable across processes and hosts."""
    return hashlib.sha256(data).hexdigest()


def put(
    tenant_id: str,
    hash_: str,
    *,
    route: Route,
    page_count: int,
    pages: dict[int, str] | None = None,
    parser_version: str = PARSER_VERSION,
    version_id: str | None = None,
) -> None:
    """Insert-if-absent. Call only after a complete, successful parse.

    Both ingestion lines call this and they call it differently. The
    feat/ingestion line passes `pages` and stores the text inline; the
    devel_jason line passes `version_id` and stored a POINTER to the already
    parsed version. The merged table (their 0024) holds the text, so a caller
    that names a version has its pages read from that version here rather than
    the two call styles needing two functions.
    """
    if pages is None:
        if version_id is None:
            raise TypeError("put() needs either `pages` or a `version_id` to read them from")
        found = pages_for_version(tenant_id, version_id)
        if found is None:
            # Not parsed, or the parse failed. Caching an empty document would
            # make every later read agree that it says nothing.
            return
        pages = dict(found.pages)
    parsed = CachedParse(
        hash=hash_,
        route=route,
        page_count=page_count,
        parser_version=parser_version,
        version_id=version_id,
        pages=pages,
    )
    payload = json.dumps({str(page): text for page, text in parsed.pages.items()})
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """
            INSERT INTO ingest_parse_artifacts
                (tenant_id, content_hash, parser_version, route, page_count, pages)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (tenant_id, content_hash, parser_version) DO NOTHING
            """,
            (tenant_id, hash_, parser_version, parsed.route.value, parsed.page_count, payload),
        )


def get(tenant_id: str, hash_: str, parser_version: str = PARSER_VERSION) -> CachedParse | None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """
            SELECT route, page_count, pages
            FROM ingest_parse_artifacts
            WHERE tenant_id = %s AND content_hash = %s AND parser_version = %s
            """,
            (tenant_id, hash_, parser_version),
        )
        row = cur.fetchone()
    if row is None:
        return None
    route_value, page_count, pages = row
    return CachedParse(
        hash=hash_,
        route=Route(route_value),
        page_count=page_count,
        parser_version=parser_version,
        pages={int(page): text for page, text in pages.items()},
    )
