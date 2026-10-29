"""Ask the model once per distinct window, ever — so a fact sheet stops moving.

    "We cannot have someone ingest the same documents more than once and have
     an unstable fact sheet. By their very nature, they are facts."

Consistency cannot come from the model. `extractors/_doc.py` records that Opus 5
and Sonnet 5 accept no `temperature`, `top_p` or `top_k` — pinning temperature
returns `400 'temperature' is deprecated for this model` — and four runs of one
144-document folder produced 678, 610, 555 and 457 facts. There is no knob.

So the model is not asked twice. `ingest/corpus.py` already hashes each window's
CONTENT (page labels excluded, so the same clause at different page numbers
still matches); this stores what came back under that hash and replays it.

  HIT  -> the stored draw. No model call, no cost, byte-identical facts.
  MISS -> call, store, return.

Three things beyond the content are in the key, each for a reason:

  * `prompt_version` — a changed instruction is a changed contract, and facts
    drawn under two contracts in one sheet is the failure nobody would notice.
    It must be bumped when the prompt changes; a test enforces that.
  * `model_id` — same argument. A different model is a different draw.
  * `deal_id` — a fact's collection key can carry deal-specific entity names
    (`its.guarantors[<party slug>]`), so a cross-deal hit would import one
    deal's key space into another. Tempting to share a standard-form building
    contract across deals; not until entity keys are canonicalised.

Tenant-scoped by row and by RLS, following `parse_cache`: a hit across tenants
would reveal that another firm holds the same document.

WHAT THIS DOES NOT DO. It makes the sheet REPEATABLE, not correct. Whichever
draw landed first is frozen, so if the underlying extraction is 60% stable the
cache does not improve that — it stops it moving. A stable wrong fact can be
found and corrected once; an unstable one cannot be corrected at all. The
underlying variance stays measurable through `ingest/determinism_gate.py`, which
must be run with the cache OFF or it measures the replay.
"""

from __future__ import annotations

import json
from typing import Any

from creditpaper.db import tenant_cursor

# Bump when the extraction instruction changes in a way that changes what comes
# back. `tests/ingest/test_fact_cache.py` fails if the prompt text changes and
# this does not, because a stale cache serving facts drawn under the previous
# contract is silent and indefinitely wrong.
PROMPT_VERSION = "2026-10-09.2"


def get(
    tenant_id: str,
    deal_id: str,
    content_hash: str,
    *,
    model_id: str,
    prompt_version: str = PROMPT_VERSION,
) -> list[dict[str, Any]] | None:
    """The stored draw for this window, or None for a miss.

    An empty list is a real answer — "this window states nothing in the field
    list" — and is NOT a miss. Returning None for it would re-ask the model
    about every empty window on every ingestion, which is most of the cost of
    not having a cache at all.
    """
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT facts FROM ingest_fact_cache "
            "WHERE tenant_id = %s AND deal_id = %s AND content_hash = %s "
            "AND prompt_version = %s AND model_id = %s",
            (tenant_id, deal_id, content_hash, prompt_version, model_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    stored = row[0]
    if isinstance(stored, str):
        stored = json.loads(stored)
    return list(stored)


def put(
    tenant_id: str,
    deal_id: str,
    content_hash: str,
    facts: list[dict[str, Any]],
    *,
    model_id: str,
    prompt_version: str = PROMPT_VERSION,
) -> None:
    """Store the draw. Idempotent, and the FIRST draw wins.

    `DO NOTHING` rather than `DO UPDATE` on purpose: two concurrent runs of the
    same deal must not race to decide which sample the sheet holds. Whoever
    inserted first is the answer, and every later reader agrees with them.
    """
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """
            INSERT INTO ingest_fact_cache
                (tenant_id, deal_id, content_hash, prompt_version, model_id, facts, fact_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, deal_id, content_hash, prompt_version, model_id)
            DO NOTHING
            """,
            (
                tenant_id,
                deal_id,
                content_hash,
                prompt_version,
                model_id,
                json.dumps(facts, default=str),
                len(facts),
            ),
        )


def invalidate(tenant_id: str, deal_id: str) -> int:
    """Drop every cached draw for one deal. Returns rows removed.

    For a deliberate re-extraction — a prompt the operator wants applied to an
    existing deal, or a sheet an analyst believes was drawn badly. Scoped to one
    deal so it can never become a global cache flush by accident.
    """
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "DELETE FROM ingest_fact_cache WHERE tenant_id = %s AND deal_id = %s",
            (tenant_id, deal_id),
        )
        return int(cur.rowcount or 0)
