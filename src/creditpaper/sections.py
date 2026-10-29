"""Section taxonomy — re-exported from `creditpaper.contracts`.

Which sections exist and what kind each is, is something the platform's navigation
needs to know, so it belongs in the contract. Kept here as a re-export because
`agents/base.py`, `profiles/` and the renderer already import it from this path.
"""

from __future__ import annotations

from creditpaper.contracts.sections import SECTION_MODES, SECTION_NUMBERS, AgentMode

__all__ = ["SECTION_MODES", "SECTION_NUMBERS", "AgentMode"]
