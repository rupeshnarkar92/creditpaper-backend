"""Proposals, confirmations, and the labelled corpus they produce.

Every confirmation is a label. That matters twice over: it is the learning loop
that lifts `neighbour_signal` for this firm's own naming conventions, and it is
the only source of document->slot ground truth in the product - the repository
has none, so routing accuracy is unmeasurable until these rows exist.

Labels are tenant-scoped in the row and by RLS. One firm's filing conventions
are not evidence about another firm's documents, and a shared corpus would leak
which documents a firm holds.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Jsonb

from creditpaper.db import tenant_cursor


def record_proposal(
    tenant_id: str,
    deal_id: str,
    *,
    content_hash: str,
    filename: str,
    outcome: str,
    slot_key: str | None,
    confidence: float,
    evidence: list[dict[str, Any]],
    needs_party_context: bool,
) -> None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.upsert_intake_proposal(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                tenant_id,
                deal_id,
                content_hash,
                filename,
                outcome,
                slot_key,
                confidence,
                Jsonb(evidence),
                needs_party_context,
            ),
        )


def list_proposals(tenant_id: str, deal_id: str) -> list[dict[str, Any]]:
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.intake_proposals(%s,%s)", (tenant_id, deal_id))
        rows = cur.fetchall()
    return [
        {
            "content_hash": r[0],
            "filename": r[1],
            "outcome": r[2],
            "slot_key": r[3],
            "confidence": float(r[4]),
            "evidence": r[5] if isinstance(r[5], list) else json.loads(r[5] or "[]"),
            "needs_party_context": bool(r[6]),
            "confirmed_slot_key": r[7],
        }
        for r in rows
    ]


def confirm_slot(tenant_id: str, deal_id: str, content_hash: str, slot_key: str, confirmed_by: str) -> None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.upsert_intake_confirmation(%s,%s,%s,%s,%s)",
            (tenant_id, deal_id, content_hash, slot_key, confirmed_by),
        )


def confirmed_labels(tenant_id: str) -> list[tuple[str, str]]:
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.confirmed_intake_labels(%s)", (tenant_id,))
        return [(r[0], r[1]) for r in cur.fetchall()]


def labelled_corpus(tenant_id: str, *, limit: int = 500) -> list[tuple[str, str]]:
    """(text, slot_key) pairs for the neighbour signal.

    A confirmation names a content hash and the parse artifact holds that
    document's page text, so this is one join.

    It used to be two: the artifact stored a POINTER at the parsed version and
    the pages were read from `deal_document_extractions`. The 2026-09-23 merge
    kept the parse cache that stores the text inline, so the artifact no longer
    has a `version_id` to join on and the second hop is gone.
    """
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.labelled_intake_corpus(%s,%s)", (tenant_id, limit))
        rows = cur.fetchall()

    corpus: list[tuple[str, str]] = []
    for page_texts, slot_key in rows:
        parsed = page_texts if isinstance(page_texts, (list, dict)) else json.loads(page_texts or "{}")
        # The parse cache stores `{page: text}`; the older extraction rows
        # stored `[{"page": n, "text": ...}]`. Both shapes are read so a
        # database written before the merge still yields a corpus.
        if isinstance(parsed, dict):
            opening = [text for _page, text in sorted(parsed.items(), key=lambda kv: int(kv[0]))[:3]]
            text = " ".join(str(t or "") for t in opening)
        else:
            entries = sorted(parsed, key=lambda entry: int(entry.get("page", 0) or 0))[:3]
            text = " ".join(str(entry.get("text") or "") for entry in entries)
        if text.strip():
            corpus.append((text, slot_key))
    return corpus


def put_held_facts(
    tenant_id: str,
    deal_id: str,
    content_hash: str,
    *,
    proposed_slot_key: str,
    facts: list[dict[str, Any]],
    coverage: dict[str, Any],
) -> None:
    """Hold speculative facts against the proposed slot (D3 as amended).

    They are not in the model, the fact sheet or any section until the analyst
    confirms the slot. Re-running extraction replaces the hold rather than
    accumulating, so a resumed job does not double it.
    """
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.upsert_held_facts(%s,%s,%s,%s,%s,%s)",
            (tenant_id, deal_id, content_hash, proposed_slot_key, Jsonb(facts), Jsonb(coverage)),
        )


def get_held_facts(
    tenant_id: str, deal_id: str, content_hash: str
) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
    """(proposed_slot_key, facts, coverage), or None when nothing is held."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT * FROM public.held_facts_row(%s,%s,%s)", (tenant_id, deal_id, content_hash))
        row = cur.fetchone()
    if not row:
        return None
    facts = row[1] if isinstance(row[1], list) else json.loads(row[1] or "[]")
    coverage = row[2] if isinstance(row[2], dict) else json.loads(row[2] or "{}")
    return str(row[0]), facts, coverage


def drop_held_facts(tenant_id: str, deal_id: str, content_hash: str) -> int:
    """Forget the hold. Returns how many rows went, so a caller can report it."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.drop_held_facts(%s,%s,%s)",
            (tenant_id, deal_id, content_hash),
        )
        return int(cur.rowcount or 0)
