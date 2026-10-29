"""The engine/platform contract — what may cross the boundary, and its version.

The engine (domains, agents, extraction, the deal model, derivation) is owned
separately from the platform (API, web app). This package is the agreed surface
between them: a platform developer asking "what may I depend on" reads one
directory.

**The dependency direction is the whole point.** `contracts` is the LOWEST layer:
it depends on nothing but `creditpaper.markup`. The engine imports from it, the
platform imports from it, and neither imports the other. Getting that backwards —
a contracts package that re-exports engine internals — would look identical from
the outside and provide none of the isolation, because a change deep in the engine
would still ripple through it.

**Versioning.** Additive changes bump the minor. Any removal or rename bumps the
major AND takes a `DECISIONS.md` entry, because it breaks a consumer the engine
team does not control. Engine internals change freely and silently; that freedom
is what this package buys.

`docs/openapi.json` and `apps/credit-web/src/api-types.ts` are generated from the
API's use of these types, and CI fails if the committed copies drift.
"""

from __future__ import annotations

from creditpaper.contracts import generation
from creditpaper.contracts.citations import (
    PATH_GRAMMAR,
    is_valid_path,
    safe_slug,
    slugify,
)
from creditpaper.contracts.prose import Authorship, ProseBlock, SectionStatus, machine_recommendations
from creditpaper.contracts.sections import SECTION_MODES, SECTION_NUMBERS, AgentMode
from creditpaper.contracts.signal import Signal, SignalKind
from creditpaper.contracts.stores import ProseStore, SignalStore

# Bump deliberately. See the module docstring: minor for additive, major for a
# removal or rename, and a major takes a DECISIONS.md entry.
# 2.1 (2026-10-07): additive — contracts.generation.
CONTRACT_VERSION = "2.1"

__all__ = [
    "CONTRACT_VERSION",
    "PATH_GRAMMAR",
    "SECTION_MODES",
    "SECTION_NUMBERS",
    "AgentMode",
    "Authorship",
    "ProseBlock",
    "ProseStore",
    "SectionStatus",
    "Signal",
    "SignalKind",
    "SignalStore",
    "generation",
    "is_valid_path",
    "machine_recommendations",
    "safe_slug",
    "slugify",
]
