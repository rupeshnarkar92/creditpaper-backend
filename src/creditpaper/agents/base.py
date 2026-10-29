"""Section agent contract (Build Plan §7).

Nine agents, one per section. Each consumes its slice of the deal model, the
House Style Profile, its section rubric, and relevant registry findings.
Agents may only reference facts that exist as model fields (D4). Voice rule:
findings, not enthusiasm. Section 9 produces a scaffold only (D8).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from creditpaper.checks.registry import CheckResult
from creditpaper.model.core import ProseBlock
from creditpaper.model.deal import DealModel

# The section taxonomy is a property of the PAPER, not of the agents that draft
# it — it lives in creditpaper.sections so tenant config and the renderer do not
# have to import the generation layer to know which sections exist. Re-exported
# because this module's public contract has always included it.
from creditpaper.sections import AgentMode


class SectionAgent(ABC):
    section: int
    mode: AgentMode

    @abstractmethod
    def draft(
        self,
        model: DealModel,
        findings: list[CheckResult],
        house_style: dict | None = None,
    ) -> ProseBlock:
        """Produce the section's prose block. Citations only to extracted fields."""
