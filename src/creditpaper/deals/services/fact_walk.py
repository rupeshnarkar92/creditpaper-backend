"""Read-only walks over a stored deal-model snapshot (the JSON in `deal_model_snapshots`).

Shared by the slot board (does any fact cite this document?), the fact sheet and
`GET /facts`. Nothing here writes a model: writes go through `model/apply.py` (D1).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

FACT_STATUSES = {"extracted", "confirmed", "flagged", "missing"}
CITABLE_STATUSES = {"extracted", "confirmed", "flagged"}

# Model keys that are not deal facts.
SKIP_TOP = {
    "prose",
    "outstanding_items",
    "model_version",
    "schema_version",
    "tenant_id",
    "deal_id",
    "created_at",
}


def is_fact(node: object) -> bool:
    return (
        isinstance(node, dict)
        and "value" in node
        and isinstance(node.get("status"), str)
        and node["status"] in FACT_STATUSES
    )


def walk_facts(model: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every Fact in the snapshot as (citation path, fact dict), in model order."""

    def _walk(node: object, path: str) -> Iterator[tuple[str, dict[str, Any]]]:
        if is_fact(node):
            assert isinstance(node, dict)
            yield path, node
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if not isinstance(key, str) or key.startswith("_"):
                    continue
                yield from _walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                yield from _walk(item, f"{path}[{i}]")

    for name, value in model.items():
        if name in SKIP_TOP:
            continue
        yield from _walk(value, name)


def cites(provenance_document_id: str | None, document: dict[str, Any]) -> bool:
    """Whether a provenance anchor points at this deal document.

    Extractors anchor to `"<routing key>:<stored filename>"` (harness ingest); other
    writers may use the document or version id. A filename shared by two documents
    counts for both, which errs towards "cited" (the safe direction for withdrawal).
    """
    if not provenance_document_id:
        return False
    ref = str(provenance_document_id)
    versions = document.get("versions") or []
    if ref == document.get("document_id") or any(ref == v.get("version_id") for v in versions):
        return True
    filenames = {v.get("original_filename") for v in versions if v.get("original_filename")}
    tail = ref.split(":", 1)[1] if ":" in ref else ref
    return tail in filenames


def citing_paths(model: dict[str, Any] | None, document: dict[str, Any]) -> list[str]:
    if not model:
        return []
    out = []
    for path, fact in walk_facts(model):
        prov = fact.get("provenance")
        if (
            fact.get("status") in CITABLE_STATUSES
            and isinstance(prov, dict)
            and cites(prov.get("document_id"), document)
        ):
            out.append(path)
    return out
