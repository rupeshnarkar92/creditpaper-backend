"""Claims-fidelity + tone review pass — the enforcement guardrail.

The D4 citation gate proves every cited path resolves; it does NOT prove a
sentence is faithful to the fact it cites, nor free of the constitution's
prohibited moves. This reviewer closes that gap: given the draft and the model's
available facts, it audits the prose against the same constitution the agent was
told to follow and returns per-sentence findings. Defense-in-depth — the engine
enforces, not the model's goodwill.

The LLM call is isolated in `_complete()` for CI. Default judge model is
`ANTHROPIC_REVIEW_MODEL` (claude-sonnet-5) — a strong, cheaper second opinion.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from creditpaper.agents.constitution import (
    PROHIBITION_CATEGORIES,
    constitution_text,
    strip_scaffold_lines,
)
from creditpaper.agents.jsonio import iter_json_objects
from creditpaper.config import review_model
from creditpaper.model.core import ProseBlock
from creditpaper.model.deal import DealModel
from creditpaper.model.flatten import citable_facts


@dataclass
class ReviewFinding:
    """One claims-fidelity violation the reviewer HOLDS for a human.

    Renamed from `ReviewFinding` ahead of the engine reshape, which introduces
    `contracts.ReviewFinding` as the central unit of analysis — a typed credit finding
    with a domain, cited paths and a severity. The two are unrelated: this is a
    complaint about a sentence, that is a conclusion about a deal. Sharing a name
    across a migration would collide in every import site and, worse, in the head
    of anyone reading either one. Renaming while there is exactly one of them is
    the cheap moment.
    """

    quote: str  # the offending text
    category: str  # one of PROHIBITION_CATEGORIES (or 'other')
    explanation: str


@dataclass
class ReviewReport:
    findings: list[ReviewFinding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings

    def as_feedback(self) -> str:
        return "\n".join(f'  - [{f.category}] "{f.quote}": {f.explanation}' for f in self.findings)


class ClaimsFidelityReviewer:
    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name

    # -- LLM boundary (override in tests) ------------------------------------
    def _complete(self, system: str, user: str) -> str:
        from creditpaper.llm.client import messages_create

        model = review_model(self._model_name)
        # Findings JSON truncated at 1500 once and killed whole sections. The
        # guard in llm/client.py now makes that loud, but keep real headroom.
        resp = messages_create(
            tool="agent:review",
            stage="review",
            model=model,
            max_tokens=48000,
            system=system,
            cache_system=True,
            messages=[{"role": "user", "content": user}],
        )
        return resp.text

    def review(self, model: DealModel, block: ProseBlock, fidelity_only: bool = True) -> ReviewReport:
        """Audit a section. Inline (the safety gate) runs `fidelity_only=True` — it
        catches fabrication / unsupported claims / rule-breaks only, and the result is
        held for the human, never auto-rewritten (auto-revision blunts fact-grounded
        judgement to dodge a flag). The ANALYSIS axis (`no_analysis`) is a drafting
        concern and an OFFLINE quality metric (`fidelity_only=False`), not an inline
        veto (DECISIONS.md 2026-06-16)."""
        facts = citable_facts(model)
        fact_lines = "\n".join(f"  {p} = {v}" for p, v in sorted(facts.items()))
        cats = ", ".join(PROHIBITION_CATEGORIES)
        if fidelity_only:
            system = (
                "You are an independent reviewer auditing a credit-paper section for "
                "FIDELITY against the firm's drafting constitution: nothing fabricated, "
                "no claim beyond the facts, no rule broken. You do NOT judge analytical "
                "depth here.\n\n" + constitution_text()
            )
        else:
            system = (
                "You are an independent reviewer auditing a credit-paper section against "
                "the firm's drafting constitution on TWO axes equally: nothing is "
                "fabricated, AND the section ANALYSES rather than merely describing.\n\n"
                + constitution_text()
            )
        # Audit only asserted prose: scaffold / analyst-action lines are marked TODO
        # templates (brace slots), not claims, and must not be read as fabrication or
        # as missing analysis on a topic the section has deliberately deferred.
        review_text = strip_scaffold_lines(block.text)
        analysis_axis = (
            ""
            if fidelity_only
            else (
                "2. ANALYSIS — flag material facts stated WITHOUT their credit reading (no "
                "implication, no 'so what', no risk/mitigant, no cross-read). Use category "
                "'no_analysis' and say what interpretation is missing.\n"
            )
        )
        cats_line = (
            f"Categories: {cats}, or 'other'.\n"
            if fidelity_only
            else f"Categories: {cats}, 'no_analysis', or 'other'.\n"
        )
        clean_note = (
            "Empty findings list if nothing is fabricated or rule-breaking."
            if fidelity_only
            else "Empty findings list if the section is clean and analytical."
        )
        user = (
            f"SECTION PROSE:\n{review_text}\n\n"
            f"AVAILABLE FACTS (path = value) — the only facts the section may rely on:\n"
            f"{fact_lines}\n\n"
            "Audit the section:\n"
            "1. FIDELITY — flag any phrase that states a fact, number, or external "
            "conclusion not derivable from the available facts, or that violates a rule.\n"
            f"{analysis_axis}"
            "Do NOT flag: (i) content that follows from any available fact (including "
            "narrative fact values); (ii) the firm's own reasoned judgement or "
            "inference FROM the facts — that is required, not a violation; (iii) "
            "general credit/construction PRINCIPLES applied to the facts (expert "
            "reasoning is not a fact claim). Only flag reasoning if it introduces a "
            "fact/number not on file or contradicts one.\n"
            "Return ONLY ACTUAL violations — do not emit a finding for content you "
            "conclude is permitted (no 'no violation / clearing' notes).\n"
            f"{cats_line}"
            'Return ONLY JSON: {"findings": [{"quote": "...", "category": "...", '
            f'"explanation": "..."}}]}}. {clean_note}'
        )
        objs = iter_json_objects(self._complete(system, user))
        # Normal case: a single {"findings": [...]} wrapper. Truncated case: the
        # wrapper didn't close, so salvage the complete finding objects directly.
        raw_findings: list[dict] = []
        for o in objs:
            if isinstance(o.get("findings"), list):
                raw_findings = o["findings"]
                break
        else:
            raw_findings = [o for o in objs if "quote" in o or "category" in o]
        return ReviewReport(
            findings=[
                ReviewFinding(
                    quote=str(f.get("quote", "")),
                    category=str(f.get("category", "other")),
                    explanation=str(f.get("explanation", "")),
                )
                for f in raw_findings
                if isinstance(f, dict)
            ]
        )
