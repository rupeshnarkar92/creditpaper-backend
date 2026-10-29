"""Read one document as windows, and hold what it says until a person confirms.

This is the entry point the window machinery was built for and never given.
`plan_windows` had no caller anywhere in `src/`, which means no document has
ever been read window-by-window in production — the capping fix that stopped a
funding table on page 16 being silently dropped lives in a module nothing ran.

Two decisions are made here and nowhere else:

  * How big is too big for one process. Past DURABLE_WINDOW_THRESHOLD the plan
    fans out as queue jobs, so a worker restart resumes rather than re-reads a
    400-page bundle. Below it, durable rows are pure overhead.
  * What "read" means. Coverage is computed from the windows actually planned,
    not from the document's page count, so a page no window covered is reported
    UNREAD rather than quietly absent. "The ITS does not state a facility limit"
    and "page 4 was never read" are different findings.

Facts go to `release.hold`, never to the model. D3 as amended (2026-09-18)
allows extraction to START on a proposed slot; it does not allow the result to
reach the paper before a person confirms the slot it was read under.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from creditpaper.ingest import parse_cache, release
from creditpaper.ingest.commit import VerifiedFact, extract_window
from creditpaper.ingest.gaps import Coverage, coverage_from
from creditpaper.ingest.page_text import PARSER_VERSION, pages_for_version
from creditpaper.ingest.pipeline_jobs import enqueue_windows, should_go_durable
from creditpaper.ingest.windows import DEFAULT_GROUPS, FieldGroup, pages_read, plan_windows
from creditpaper.llm.registry import Task, resolve


class DocumentRun(BaseModel):
    content_hash: str
    windows: int
    facts: int
    durable: bool
    job_ids: list[str] = []
    coverage: Coverage


def _groups(slot_key: str) -> tuple[FieldGroup, ...]:
    """Exactly one group per extractor that reads this slot.

    `slot_key` arrives in the BOARD's key-space and the window groups are in the
    extractors': `signed_its` has to become `its` before anything matches.

    One group per routing key, never more. A group is what `extract_window`
    turns into an extractor run, so returning every group would read one
    document once per group — six documents became ten reads that way, and the
    concurrency it was meant to demonstrate disappeared under the duplicated
    serial work inside each document.

    A routing key with no entry in DEFAULT_GROUPS still gets a group, with no
    hints: `plan_windows` then falls back to the opening pages for it, which is
    reading less than we would like rather than reading nothing at all.
    """
    from creditpaper.deals.services.slot_matcher import routing_keys_for

    wanted = routing_keys_for(slot_key)
    if not wanted:
        # No extractor claims this slot. Read it against everything rather than
        # not at all; `speculate.py` skips these before they reach here.
        return DEFAULT_GROUPS

    by_name = {g.name: g for g in DEFAULT_GROUPS}
    return tuple(by_name.get(key) or FieldGroup(name=key, hints=()) for key in wanted)


def run_document(
    tenant_id: str,
    deal_id: str,
    *,
    version_id: str,
    content_hash: str,
    proposed_slot_key: str,
    document_id: str | None = None,
    registry: dict[str, type] | None = None,
) -> DocumentRun:
    parse = parse_cache.get(tenant_id, content_hash)
    if parse is None:
        # The extraction row is authoritative and can legitimately predate the
        # derived parse cache (for example, a server-side extraction-cache hit
        # or a deployment made while an archive match was already running).
        # Repair that cache boundary here instead of dropping an otherwise
        # ready document from speculative fact extraction.
        extracted = pages_for_version(tenant_id, version_id)
        if extracted is None:
            raise LookupError(f"no parsed extraction for {content_hash[:12]}")
        parse_cache.put(
            tenant_id,
            content_hash,
            route=extracted.route,
            page_count=extracted.page_count,
            pages=extracted.pages,
            parser_version=PARSER_VERSION,
            version_id=version_id,
        )
        parse = parse_cache.CachedParse(
            hash=content_hash,
            route=extracted.route,
            page_count=extracted.page_count,
            parser_version=PARSER_VERSION,
            version_id=version_id,
            pages=extracted.pages,
        )

    payload: dict[str, Any] = {
        "content_hash": content_hash,
        "version_id": version_id,
        "document_id": document_id or version_id,
    }
    windows = plan_windows(parse, _groups(proposed_slot_key), resolve(Task.EXTRACT))
    requested = tuple(sorted(parse.pages))

    if should_go_durable(windows):
        job_ids = enqueue_windows(
            tenant_id,
            deal_id,
            content_hash=content_hash,
            extractor_version=proposed_slot_key,
            windows=windows,
        )
        # Nothing has been read yet, so nothing is held and no page is claimed
        # as covered. Each window job holds its own result as it lands.
        return DocumentRun(
            content_hash=content_hash,
            windows=len(windows),
            facts=0,
            durable=True,
            job_ids=job_ids,
            coverage=coverage_from(requested, ()),
        )

    facts: list[VerifiedFact] = []
    for window in windows:
        facts.extend(extract_window(tenant_id, deal_id, payload, window, registry=registry))

    coverage = coverage_from(requested, pages_read(windows))
    release.hold(tenant_id, deal_id, content_hash, proposed_slot_key, facts, coverage)
    return DocumentRun(
        content_hash=content_hash,
        windows=len(windows),
        facts=len(facts),
        durable=False,
        coverage=coverage,
    )
