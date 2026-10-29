"""Learn a per-section 'house playbook' from a client's prior approved papers (D10).

The product thesis: send us your signed papers, get a paper in your house voice.
This is the learning step. For each section, we slice the corresponding section out
of every prior paper and distil the RECURRING PRINCIPLES — structure and subsection
ordering, the analysis moves that recur, how risks/conclusions are framed, edge
cases by deal type, and anti-patterns to avoid. The distillate is a compact playbook
stored in the House Style Profile and injected into that section's agent.

Two hard rules (DECISIONS.md 2026-06-16):
  - Principles, NOT specifics. The playbook captures form and reasoning patterns,
    never a deal's facts/figures/names — content comes only from the deal model (D4).
  - Do NOT regress to the corpus mean. The prior papers are the old human bar; the
    constitution's analysis mandate is a HIGHER bar on depth. The playbook governs
    shape and rigour; the agent still out-analyses the corpus.

The corpus must exclude the build deal and the blind validation deals (no Track-2
contamination). The LLM call is isolated in `_complete()` for CI (no network/key).
"""

from __future__ import annotations

import re

from creditpaper.config import prose_model
from creditpaper.contracts.sections import SECTION_KEYWORDS
from creditpaper.profiles.house_style import CANONICAL_3CAPITAL_SECTIONS

# Heading keywords used to find section boundaries in an extracted prior paper.
# Boundary detection uses ALL of them (incl. the table sections 1/3); we distil
# the prose sections. Matched case-insensitively at (roughly) a line start. The map
# lives in `contracts.sections` so the platform's section resolver shares it.

# Sections we distil playbooks for (prose sections; 1 and 3 are deterministic tables).
PLAYBOOK_SECTIONS = [2, 4, 5, 6, 7, 8, 9]

_HEADING = r"^[^\S\n]*(?:\d+[.)]?[^\S\n]*)?(?:%s)\b.*$"


def _first_heading_pos(text: str, keyword: str) -> int | None:
    m = re.search(_HEADING % keyword, text, re.IGNORECASE | re.MULTILINE)
    return m.start() if m else None


def slice_paper_sections(text: str) -> dict[int, str]:
    """Split one prior paper's extracted text into {section: text} by locating each
    canonical section heading and slicing to the next heading. Tolerant: a section
    whose heading is not found is omitted, and bounds need not be perfect.

    **That tolerance is corpus-size dependent, and the corpus got smaller.** The
    original note here said the signal is robust "because the distiller reads across
    many papers" — true at fifteen, where one ragged slice is a fifteenth of the
    evidence. The banked corpus is **~3 papers plus the website** (`DECISIONS.md`
    2026-06-16), where a ragged slice is a third of it, and a heading this function
    fails to find contributes nothing at all.

    The consequence is a live onboarding failure mode: a tenant whose papers use
    headings these keywords do not match yields **no slices for that section**, and
    `learn_playbooks` then omits it from the playbook silently. At fifteen papers that
    self-corrects; at three it does not. Callers must report coverage rather than
    assume it — see `scripts/learn_house_style.py`."""
    marks: list[tuple[int, int]] = []  # (position, section)
    for section, keyword in SECTION_KEYWORDS.items():
        pos = _first_heading_pos(text, keyword)
        if pos is not None:
            marks.append((pos, section))
    marks.sort()
    out: dict[int, str] = {}
    for i, (pos, section) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out[section] = text[pos:end].strip()
    return out


def section_slices(papers: dict[str, str], section: int, cap: int = 3000) -> list[str]:
    """The given section, sliced from every paper that has it (capped per paper)."""
    slices: list[str] = []
    for text in papers.values():
        sec = slice_paper_sections(text).get(section)
        if sec and len(sec) > 200:  # ignore empty / heading-only slices
            slices.append(sec[:cap])
    return slices


_TITLES = {s.section: s.title.title() for s in CANONICAL_3CAPITAL_SECTIONS}


class HouseStyleLearner:
    """Distils a section playbook from many prior versions of that section."""

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name

    def _complete(self, system: str, user: str) -> str:
        from creditpaper.llm.client import messages_create

        model = prose_model(self._model_name)
        resp = messages_create(
            tool="harness:house_style",
            stage="house_style",
            model=model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return resp.text

    def _system(self) -> str:
        return (
            "You study a single firm's prior APPROVED credit papers to learn how that "
            "firm writes one section, so a drafting agent can follow the house pattern. "
            "You extract PRINCIPLES AND PATTERNS ONLY — never a deal's specific facts, "
            "figures, names, or addresses. Two rules: (1) principles not specifics; (2) "
            "do not homogenise — identify the STRONGEST recurring practice and call out "
            "ANTI-PATTERNS (vague, qualitative, or weak moves) to avoid. The reader of "
            "your playbook already has a higher analytical bar than these papers, so "
            "capture structure and rigour, not a lowest-common-denominator template."
        )

    def distill_section(self, section: int, slices: list[str]) -> str:
        title = _TITLES.get(section, f"Section {section}")
        joined = "\n\n--- NEXT PAPER ---\n\n".join(
            f"[PRIOR PAPER {i + 1}]\n{s}" for i, s in enumerate(slices)
        )
        user = (
            f"Here are the '{title}' sections from {len(slices)} prior approved papers "
            f"by one firm, separated by markers.\n\n{joined}\n\n"
            f"Produce the HOUSE PLAYBOOK for the '{title}' section as compact guidance a "
            "drafting agent will follow. Cover, in plain prose with short headers:\n"
            "  - STRUCTURE: the subsections and their typical order.\n"
            "  - ANALYSIS MOVES: what the firm consistently computes, interprets, or "
            "tests in this section (the reasoning, not the numbers).\n"
            "  - FRAMING: how findings/risks/conclusions are characterised and attributed.\n"
            "  - EDGE CASES: how the section differs by deal type (construction, residual "
            "stock, bridge) where the papers show it.\n"
            "  - ANTI-PATTERNS: weak or vague moves seen in the corpus to AVOID.\n"
            "Do NOT include any specific deal's facts, figures, parties, or addresses. "
            "Keep it under 400 words."
        )
        return self._complete(self._system(), user).strip()


def learn_playbooks(
    papers: dict[str, str],
    sections: list[int] | None = None,
    learner: HouseStyleLearner | None = None,
) -> dict[int, str]:
    """Distil a playbook per section from the corpus. `papers` is {name: text} of the
    prior papers (caller is responsible for excluding the build + blind deals)."""
    learner = learner or HouseStyleLearner()
    out: dict[int, str] = {}
    for section in sections or PLAYBOOK_SECTIONS:
        slices = section_slices(papers, section)
        if slices:
            out[section] = learner.distill_section(section, slices)
    return out
