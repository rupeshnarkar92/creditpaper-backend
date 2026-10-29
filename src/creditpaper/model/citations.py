"""D4 enforcement on the agent side, and the citation-path grammar.

Citable = has provenance (DECISIONS.md 2026-06-11): a prose block may cite
fields that are `extracted`, `confirmed`, or `flagged` — never `missing` or
unresolvable. A citation to a `flagged` fact is a DISCLOSURE citation: the
renderer must present it as a finding, not a neutral fact (the inverse —
flagged facts that are never disclosed — is registry check #17).

The path grammar is fixed here (DECISIONS.md 2026-06-11) and malformed paths
are rejected at write time, not left to fail silently at resolution:

    path     = segment ("." segment)*
    segment  = name ("[" key "]")?
    name,key = snake_case: [a-z][a-z0-9_]*

e.g. `sop[j_smith].net_position`, `economics.fees[establishment].pct`.
Keys are bare slugs — no quotes, no type prefixes. Generate them with
`slugify` so independent extractors spell the same key the same way.
"""

from __future__ import annotations

from typing import Any

from creditpaper.model.core import Fact, FactStatus, ProseBlock
from creditpaper.model.deal import DealModel

CITABLE = {FactStatus.EXTRACTED, FactStatus.CONFIRMED, FactStatus.FLAGGED}

# The GRAMMAR is published in `creditpaper.contracts.citations` — a platform
# screen validates a path before sending it, and builds keys the same way the
# engine does. RESOLUTION stays here: walking a `DealModel` is an engine internal
# that will change when findings replace prose blocks.
from creditpaper.contracts.citations import (  # noqa: E402
    PATH_GRAMMAR,
    is_valid_path,
    safe_slug,
    slugify,
)

CITABLE = {FactStatus.EXTRACTED, FactStatus.CONFIRMED, FactStatus.FLAGGED}

# Re-exported so the engine's long-standing import site keeps working. Declared in
# __all__ rather than marked noqa: an autofixer removed these as "unused" on the
# first pass, which is what an unexplained re-export looks like to a tool.
__all__ = [
    "CITABLE",
    "PATH_GRAMMAR",
    "disclosure_citations",
    "invalid_citations",
    "is_valid_path",
    "resolve_path",
    "safe_slug",
    "slugify",
]


def resolve_path(model: DealModel, path: str) -> Any:
    """Resolve a grammar-valid dotted path with [key] segments against the
    model. Returns the object at the path, or None if it does not resolve.
    Malformed paths never resolve (and should have been rejected at write)."""
    if not is_valid_path(path):
        return None
    obj: Any = model
    for part in path.split("."):
        if obj is None:
            return None
        name, _, key = part.partition("[")
        obj = getattr(obj, name, None) if not isinstance(obj, dict) else obj.get(name)
        if key:  # trailing "]" present by grammar
            if not isinstance(obj, dict):
                return None
            obj = obj.get(key[:-1])
    return obj


def invalid_citations(model: DealModel, block: ProseBlock) -> list[str]:
    """Return the cited paths that violate D4: malformed, unresolvable, or
    resolving to a Fact without provenance (missing). Empty list == block may
    advance."""
    bad: list[str] = []
    for path in block.cited_field_paths:
        target = resolve_path(model, path)
        # A citation must land ON A FACT. Resolving to *something* is not enough:
        # this branch used to reject only `None` and non-citable Facts, so a block
        # could cite `thesis.overall_read` — the engine's own unanchored prose,
        # which by design carries no provenance — or a whole container like
        # `economics`, and clear the gate that is the product's central claim.
        if not isinstance(target, Fact):
            bad.append(path)
        elif target.status not in CITABLE:
            bad.append(path)
    return bad


def disclosure_citations(model: DealModel, block: ProseBlock) -> list[str]:
    """The cited paths that resolve to `flagged` facts. The renderer must
    present these as disclosures/findings, never as neutral facts."""
    out: list[str] = []
    for path in block.cited_field_paths:
        target = resolve_path(model, path)
        if isinstance(target, Fact) and target.status == FactStatus.FLAGGED:
            out.append(path)
    return out
