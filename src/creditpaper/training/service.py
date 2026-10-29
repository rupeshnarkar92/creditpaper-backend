"""Extraction training service — captures extraction results for improvement.

Stores extraction examples with expected vs actual output, enabling:
1. Quality scoring of extraction results
2. Prompt improvement based on real data
3. Version tracking of instruction changes
4. A/B testing of extractor configurations
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from creditpaper.db import cursor, tenant_cursor, tenant_uuid


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_training_example(
    tenant_id: str,
    deal_id: str,
    document_id: str,
    slot_key: str,
    model_used: str,
    instruction_text: str,
    input_pdf_path: Path,
    expected_output: dict[str, Any],
    actual_output: dict[str, Any],
    flags: list[str],
    quality_score: float | None = None,
    notes: str | None = None,
) -> str:
    """Save an extraction training example to the DB.

    Returns the example ID.
    """
    instruction_hash = _hash_text(instruction_text)
    pdf_hash = _hash_file(input_pdf_path)

    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT public.save_training_example(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                tenant_uuid(tenant_id),
                deal_id,
                document_id,
                slot_key,
                model_used,
                instruction_hash,
                pdf_hash,
                json.dumps(expected_output),
                json.dumps(actual_output),
                json.dumps(flags),
                quality_score,
                notes,
            ),
        )
        return str(cur.fetchone()[0])


def save_prompt_version(
    slot_key: str,
    version: str,
    instruction_text: str,
    change_reason: str | None = None,
) -> str:
    """Save a new version of an extraction prompt.

    Returns the version ID.
    """
    instruction_hash = _hash_text(instruction_text)

    with cursor() as cur:
        cur.execute(
            "SELECT public.save_prompt_version(%s,%s,%s,%s,%s)",
            (slot_key, version, instruction_text, instruction_hash, change_reason),
        )
        return str(cur.fetchone()[0])


def get_training_examples(
    slot_key: str | None = None,
    min_quality_score: float | None = None,
    limit: int = 100,
) -> list[dict]:
    """Get training examples for analysis."""
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM public.get_training_examples(%s,%s,%s)",
            (slot_key, min_quality_score, limit),
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def get_extraction_stats() -> dict:
    """Get extraction quality statistics."""
    with cursor() as cur:
        cur.execute("SELECT * FROM public.get_extraction_stats()")
        cols = [desc[0] for desc in cur.description]
        return {row[0]: dict(zip(cols[1:], row[1:], strict=False)) for row in cur.fetchall()}


def get_prompt_versions(slot_key: str) -> list[dict]:
    """Get all versions of an extraction prompt."""
    with cursor() as cur:
        cur.execute("SELECT * FROM public.get_prompt_versions(%s)", (slot_key,))
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
