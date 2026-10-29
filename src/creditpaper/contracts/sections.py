"""The paper's section taxonomy: which sections exist, and what kind each is.

This is a property of the PAPER, not of the agents that happen to draft it today.
It lived in `agents/base.py`, so `profiles/house_style.py` (tenant configuration)
imported it from the generation layer, and `renderer/docx_render.py` imported
`profiles` — making the whole chain renderer → profiles → agents move together.

That chain is a migration hazard, not just an aesthetic one. The 2026-08-21
architecture spec replaces the seven section-agent classes with `SectionSpec`
data and deletes `agents/base.py` with them. Left as it was, deleting the agent
contract would take tenant configuration and the renderer down with it.

Depends on nothing. Everything that needs the taxonomy — the agents, the profile,
the renderer, and the future `SectionSpec` registry — imports it from here.
"""

from __future__ import annotations

from enum import Enum


class AgentMode(str, Enum):
    """How a section is produced.

    Survives into the reshape as a property of a `SectionSpec`: the spec's split
    is between DETERMINISTIC sections (rendered from the model, no LLM) and
    rendered ones, which is this distinction with the middle tiers collapsed.
    """

    DETERMINISTIC = "deterministic"  # sections 1, 3 — native tables from the model
    ANALYTICAL = "analytical"  # section 6
    JUDGEMENT = "judgement"  # sections 2, 5, 7
    HYBRID = "hybrid"  # sections 4, 8
    SCAFFOLD = "scaffold"  # section 9 — D8: the recommendation is human-authored


SECTION_MODES: dict[int, AgentMode] = {
    1: AgentMode.DETERMINISTIC,
    2: AgentMode.JUDGEMENT,
    3: AgentMode.DETERMINISTIC,
    4: AgentMode.HYBRID,
    5: AgentMode.JUDGEMENT,
    6: AgentMode.ANALYTICAL,
    7: AgentMode.JUDGEMENT,
    8: AgentMode.HYBRID,
    9: AgentMode.SCAFFOLD,
}

SECTION_NUMBERS: tuple[int, ...] = tuple(sorted(SECTION_MODES))

# Heading keywords that identify each engine section in a paper's headings. Matched
# case-insensitively. Shared by the house-style learner (slicing prior papers) and the
# platform's interim section resolver (mapping a firm template's headings onto the
# engine sections that can write them). A regex per section, first match wins.
SECTION_KEYWORDS: dict[int, str] = {
    1: r"deal snapshot",
    2: r"deal summary",
    3: r"funding snapshot|funding (overview|table|position)",
    4: r"zoning|location",
    5: r"borrower analysis|sponsor analysis",
    6: r"valuation analysis|valuation assessment",
    7: r"risks?\b.*mitigation|key risks?",
    8: r"other considerations|security (structure|and covenants)",
    9: r"conclusion|recommendation",
}
