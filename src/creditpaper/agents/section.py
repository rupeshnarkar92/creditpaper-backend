"""LLM-backed section agents (Build Plan §7).

An agent consumes its slice of the deal model (as citable {path: value} facts),
the House Style Profile, its rubric, and registry findings, and drafts a prose
block. D4 is enforced structurally: the agent is given the exact set of citable
paths and may cite only those; every draft is run through the citation gate, and
a draft that cites anything unresolvable/missing is repaired once, then its bad
citations are dropped. Voice rule: findings, not enthusiasm.

The LLM call is isolated in `_complete()` so tests can subclass with a canned
response (no network, no key in CI). Production uses the Anthropic API (the
approved M1 path); the prose model is `ANTHROPIC_PROSE_MODEL` (default
claude-opus-5).
"""

from __future__ import annotations

import calendar
import re
from datetime import date

from creditpaper.agents.base import AgentMode, SectionAgent
from creditpaper.agents.constitution import constitution_text
from creditpaper.agents.review import ClaimsFidelityReviewer, ReviewReport
from creditpaper.checks.registry import CheckResult
from creditpaper.config import prose_model
from creditpaper.guardrails.services.guardrail_service import firm_rules_text
from creditpaper.model.citations import invalid_citations, is_valid_path
from creditpaper.model.core import ProseBlock, SectionStatus
from creditpaper.model.deal import DealModel
from creditpaper.model.flatten import citable_facts

# Which section owns the full analysis of each topic — injected into every agent so
# cross-references point to the right place and no topic is analysed twice (round 1).
SECTION_OWNERSHIP: dict[int, str] = {
    1: "Deal Snapshot — the at-a-glance facts table",
    2: "Deal Summary — what the deal IS, project scope, parties, status, exit thesis",
    3: "Funding Snapshot — the sources-and-uses funding table",
    4: "Zoning & Location — site, title, zoning/planning, DA, environmental, locality fit",
    5: "Borrower Analysis — sponsor track record, structure/guarantees, capacity, builder",
    6: "Valuation Analysis — valuer/basis, values, comps, LVR and its headroom, currency",
    7: "Risks & Mitigations — the consolidated risk register",
    8: "Other Considerations — security structure, covenants in detail, screening, insurance",
    9: "Conclusion — synthesis of the thesis and the (human-authored) recommendation",
}


def _paper_map(current: int) -> str:
    lines = "\n".join(
        f"  - Section {n}{' (THIS SECTION)' if n == current else ''}: {desc}"
        for n, desc in SECTION_OWNERSHIP.items()
    )
    return (
        "PAPER MAP — each topic is analysed in full in ONE owning section. Analyse "
        "what THIS section owns; for anything owned elsewhere, make the point in one "
        "sentence and add '(see Section N)', do not re-derive it:\n" + lines
    )


_OUTPUT_FORMAT = (
    "OUTPUT FORMAT — plain text, NOT JSON:\n"
    "Write the section body first (with '## ' subsection headings as instructed). "
    "Then a line containing only '---CITATIONS---'. Then list, one per line, every "
    "deal-model path you relied on, copied EXACTLY from the AVAILABLE FACTS list. "
    "Write nothing after the citation list.\n"
    "Do NOT write deal-model paths inline in the prose (e.g. "
    "'(valuation.market.supply_demand)') — provenance is rendered separately as "
    "numbered references from the citation list; inline paths are raw schema and "
    "are stripped before render."
)


def citations_from_prose(prose: str, facts: dict[str, str]) -> list[str]:
    """Deterministically recover which facts a section relied on by matching each
    fact's value against the prose — robust provenance that does not depend on the
    model remembering to list citations. Numbers match with or without thousands
    separators; dates match ISO or full form; strings match as substrings."""
    digits = re.sub(r"[,\s]", "", prose)
    found: list[str] = []
    for path, raw in facts.items():
        v = raw.strip()
        hit = False
        if re.fullmatch(r"-?\d+(\.\d+)?", v):  # numeric
            if len(v.replace(".", "").lstrip("-")) >= 3 and v in digits:
                hit = True
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):  # ISO date
            try:
                d = date.fromisoformat(v)
                full = f"{d.day} {calendar.month_name[d.month]} {d.year}"
                hit = v in prose or full in prose
            except ValueError:
                hit = False
        elif len(v) >= 5:  # string (avoid short noisy tokens)
            hit = v in prose
        if hit:
            found.append(path)
    return found


# Inline citation-path tokens (e.g. "(valuation.market.supply_demand)") sometimes
# leak into the prose as the model's own attribution. The numbered provenance refs
# already carry traceability, so these raw schema paths are redundant clutter in a
# committee-facing document — strip them, harvesting any into the citation set first
# so a path expressed ONLY inline still reaches provenance. We only remove a
# parenthesised group whose entire content is path tokens, so ordinary prose
# parentheticals ("(see Section 6)", "(DCF)", "(e.g. ...)") are never touched.
# A path NAME segment is >=2 chars (so prose abbreviations 'i.e', 'e.g', 'c.f'
# never read as paths); KEYs are bracketed slugs. A full PATH_TOKEN has at least
# one continuation (a '.name' or '[key]') — that is what marks it as schema, not a
# bare word. A group ELEMENT may be a bare name too, because the model abbreviates
# shared prefixes, e.g. "(valuation.market.locality_outlook, supply_demand)".
_NAME = r"[a-z][a-z0-9_]+"
_KEY = r"\[[a-z][a-z0-9_]*\]"
_PATH_TOKEN = rf"{_NAME}(?:{_KEY}|\.{_NAME}(?:{_KEY})?)+"
_ELEM = rf"{_NAME}(?:{_KEY}|\.{_NAME}(?:{_KEY})?)*"
_INLINE_PATH_GROUP = re.compile(rf"[ \t]*\(\s*{_ELEM}(?:\s*,\s*{_ELEM})*\s*\)")
_PATH_TOKEN_RE = re.compile(_PATH_TOKEN)


def strip_inline_citation_paths(text: str) -> tuple[str, list[str]]:
    """Remove parenthesised groups that are citation paths (e.g.
    '(valuation.market.supply_demand)'), and return the cleaned prose plus the
    full paths harvested from them. A group is only removed when it holds at least
    one real path token, so plain-word parentheticals ('(net, gross)', '(see
    Section 6)', '(i.e., ...)') are left untouched."""
    harvested: list[str] = []

    def _drop(m: re.Match) -> str:
        paths = _PATH_TOKEN_RE.findall(m.group(0))
        if not paths:  # a parenthetical of plain words — not a citation, keep it
            return m.group(0)
        harvested.extend(paths)
        return ""

    cleaned = _INLINE_PATH_GROUP.sub(_drop, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([.,;:])", r"\1", cleaned)
    return cleaned, list(dict.fromkeys(harvested))


class LLMSectionAgent(SectionAgent):
    section: int
    mode: AgentMode
    title: str = ""
    rubric: str = ""  # the section's rubric text, injected to steer the draft
    task: str = ""  # section-specific instruction

    def __init__(self, model_name: str | None = None, tenant_id: str | None = None) -> None:
        self._model_name = model_name  # resolved lazily so CI/tests need no key
        self._tenant_id = tenant_id

    # -- LLM boundary (override in tests) ------------------------------------
    def _complete(self, system: str, user: str) -> str:
        from creditpaper.llm.client import messages_create

        model = prose_model(self._model_name)
        resp = messages_create(
            tool=f"agent:section{getattr(self, 'section', '')}",
            stage="section",
            # 16000, not 8000: at 8000 the most material section (§6 valuation) was
            # truncated mid-sentence. Concision is enforced by the constitution, not
            # by a token ceiling; this is a safety headroom, not a target length.
            model=model,
            max_tokens=64000,
            system=system,
            cache_system=True,
            messages=[{"role": "user", "content": user}],
        )
        return resp.text

    # -- prompt construction --------------------------------------------------
    brief: str = ""  # per-section expert methodology (the SME 'how')
    format_note: str = (
        "Structure the section into subsections: begin each subsection heading line "
        "with '## ' (the heading TITLE only — do NOT number headings yourself; the "
        "renderer applies numbering). Use '### ' for any sub-subheading. Every other "
        "line is body text. Do not use inline bold lead-ins as headings.\n"
        "For comparative / multi-attribute data, emit a Markdown pipe-table (header "
        "row, a '| --- |' separator row, then data rows); the renderer converts it to "
        "a native Word table. Keep prose analysis around the table, not inside it.\n"
        "For any material element NOT supported by the available facts, use the "
        "scaffold convention from the constitution: '>> ' lines for best-practice "
        "template structure with {brace} slots, and '[ANALYST] ' lines for the human "
        "action. Do not write prose null-statements ('… is outstanding')."
    )

    def _system(self, house_style: dict | None) -> str:
        hs = house_style or {}
        client = hs.get("client", "the client")
        title = hs.get("paper_title", "Internal Credit Memorandum")
        voice = hs.get("voice") or {}
        voice_block = ""
        if voice:
            voice_lines = "\n".join(f"  - {k}: {v}" for k, v in voice.items())
            voice_block = f"\n\nHouse voice ({client}):\n{voice_lines}"
        brief_block = f"\n\nExpert method for this section:\n{self.brief}" if self.brief else ""
        firm_block = ""
        generation_rules = getattr(self, "_generation_guardrail_rules", None)
        if generation_rules is not None:
            from creditpaper.guardrails.services.guardrail_service import describe

            if generation_rules:
                lines = [f"- [{rule['kind']}] {describe(rule)}" for rule in generation_rules]
                firm_block = "\n\nFirm rules (captured for this generation):\n" + "\n".join(lines)
        elif self._tenant_id:
            fr = firm_rules_text(self._tenant_id, "deals")
            if fr:
                firm_block = f"\n\n{fr}"
        return (
            f"You are a senior credit analyst drafting the '{self.title}' section of "
            f"{client}'s {title}.\n\n{constitution_text()}{firm_block}"
            f"{brief_block}{voice_block}\n\n"
            f"Rubric this section is scored against:\n{self.rubric}"
        )

    def _user(
        self,
        facts: dict[str, str],
        findings: list[CheckResult],
        house_style: dict | None = None,
        revision_notes: str | None = None,
        thesis_brief: str = "",
    ) -> str:
        fact_lines = "\n".join(f"  {p} = {v}" for p, v in sorted(facts.items()))
        finding_lines = (
            "\n".join(f"  - {f.check_key}: {'PASS' if f.passed else 'FLAG'} — {f.message}" for f in findings)
            or "  (none)"
        )
        hs = house_style or {}
        playbook = hs.get("section_playbooks", {}).get(self.section) or hs.get("section_playbooks", {}).get(
            str(self.section)
        )
        playbook_block = ""
        if playbook:
            playbook_block = (
                "\n\nHOUSE PLAYBOOK — distilled from the firm's prior approved papers "
                "for THIS section (structure, analysis moves, framing, anti-patterns). "
                "Follow the house pattern and rigour, but do NOT regress to it: your "
                "analysis mandate is the higher bar, and content comes only from THIS "
                f"deal's AVAILABLE FACTS:\n---\n{playbook.strip()}\n---\n"
            )
        exemplar = hs.get("section_exemplars", {}).get(self.section) or hs.get("section_exemplars", {}).get(
            str(self.section)
        )
        exemplar_block = ""
        if exemplar:
            exemplar_block = (
                "\n\nFORM REFERENCE — one prior, unrelated deal's section, for concrete "
                "shape ONLY. Do NOT copy its facts; use only THIS deal's AVAILABLE "
                f"FACTS:\n---\n{exemplar.strip()[:2000]}\n---\n"
            )
        revision = ""
        if revision_notes:
            revision = (
                "\n\nAN INDEPENDENT REVIEW flagged the issues below in your previous "
                "draft. Re-draft to fix every one, keeping everything else and the "
                f"citation discipline:\n{revision_notes}\n"
            )
        thesis_block = f"\n\n{thesis_brief}\n" if thesis_brief else ""
        return (
            f"{self.task}\n\n{_paper_map(self.section)}\n\n{self.format_note}\n"
            f"{thesis_block}{playbook_block}{exemplar_block}\n"
            f"AVAILABLE FACTS (path = value) — cite only from these:\n{fact_lines}\n\n"
            f"REGISTRY FINDINGS:\n{finding_lines}\n"
            f"{revision}\n"
            f"{_OUTPUT_FORMAT}"
        )

    @staticmethod
    def _normalise_citation(line: str) -> str | None:
        """A citation path stripped of list decoration, or None if unusable.

        Models format lists. "1. economics.facility_amount", "- valuation.valuer"
        and "`economics.term_months`," are all a correct path wearing markup, and
        every one of them failed the grammar — which triggered a repair draft, then
        a no-citation retry, so up to THREE Opus calls per section, and a block
        that could still end with no provenance. The prompt never forbade the
        formatting, so normalising is the honest fix rather than asking harder.
        """
        s = line.strip().strip("`\"'")
        s = re.sub(r"^[-*•]\s+", "", s)  # bullet
        s = re.sub(r"^\d+[.)]\s+", "", s)  # numbered
        s = s.strip().strip("`\"'").rstrip(".,;")
        return s if s and is_valid_path(s) else None

    @staticmethod
    def _parse(raw: str) -> tuple[str, list[str]]:
        text = re.sub(r"^```[a-zA-Z]*\s*", "", raw.strip())
        text = re.sub(r"\s*```$", "", text).strip()
        marker = "---CITATIONS---"
        if marker in text:
            prose, _, cites = text.partition(marker)
            listed = [p for p in (LLMSectionAgent._normalise_citation(c) for c in cites.splitlines()) if p]
        else:
            prose, listed = text, []
        prose, harvested = strip_inline_citation_paths(prose.strip())
        return prose, list(dict.fromkeys(listed + harvested))

    def draft(
        self,
        model: DealModel,
        findings: list[CheckResult],
        house_style: dict | None = None,
        revision_notes: str | None = None,
    ) -> ProseBlock:
        facts = citable_facts(model)
        thesis_brief = model.thesis.as_brief() if model.thesis.present else ""
        system = self._system(house_style)
        user = self._user(facts, findings, house_style, revision_notes, thesis_brief)

        prose, citations = self._parse(self._complete(system, user))
        # Merge model-listed citations with deterministic value-matching, so a
        # dropped citation list never costs us D4 traceability.
        merged = list(dict.fromkeys(citations + citations_from_prose(prose, facts)))
        block = ProseBlock(
            section=self.section, text=prose, cited_field_paths=merged, status=SectionStatus.DRAFT
        )

        bad = invalid_citations(model, block)
        if bad:
            repair = (
                f"{user}\n\nYour previous citations included paths that are not in "
                f"AVAILABLE FACTS: {bad}. Re-draft, citing ONLY paths from the list."
            )
            prose, citations = self._parse(self._complete(system, repair))
            # Merge deterministic value-matching here TOO. The first draft does it
            # (see above) but the repair did not, so a repair that came back with a
            # thinner citation list cost the section every value-matched path it
            # already had — the fix for one D4 gap opening another.
            repaired = ProseBlock(
                section=self.section,
                text=prose,
                cited_field_paths=list(dict.fromkeys(citations + citations_from_prose(prose, facts))),
                status=SectionStatus.DRAFT,
            )
            # backstop: drop any citation still invalid so the block obeys D4.
            still_bad = set(invalid_citations(model, repaired))
            repaired.cited_field_paths = [c for c in repaired.cited_field_paths if c not in still_bad]
            # Only adopt the repair if it actually has traceability. Replacing a
            # cited block with an uncited one is a worse outcome than the invalid
            # path we were repairing — the same rule the retry path already applies.
            if repaired.cited_field_paths:
                block = repaired

        # A substantive section with no citations is a D4 traceability gap — retry
        # once demanding citations (covers a first draft that returned none).
        if facts and not block.cited_field_paths and len(block.text.strip()) > 200:
            retry = (
                f"{user}\n\nYour response listed NO citations after '---CITATIONS---'. "
                "Every material claim MUST rest on a path copied exactly from AVAILABLE "
                "FACTS. Re-draft the same section with the citation list populated."
            )
            prose, citations = self._parse(self._complete(system, retry))
            candidate = ProseBlock(
                section=self.section,
                text=prose,
                cited_field_paths=list(dict.fromkeys(citations + citations_from_prose(prose, facts))),
                status=SectionStatus.DRAFT,
            )
            bad = set(invalid_citations(model, candidate))
            candidate.cited_field_paths = [c for c in candidate.cited_field_paths if c not in bad]
            if candidate.cited_field_paths:  # only accept if it now has traceability
                block = candidate
        return block

    def draft_and_review(
        self,
        model: DealModel,
        findings: list[CheckResult],
        house_style: dict | None,
        reviewer: ClaimsFidelityReviewer,
    ) -> tuple[ProseBlock, ReviewReport]:
        """Draft, then run the FIDELITY gate (the safety check) and HOLD any findings
        for the human — the block is NOT auto-rewritten. Auto-revision blunts
        fact-grounded judgement to dodge a flag, and the right fix for a recurring
        flag is upstream (drafting prompt + the learning loop), not a second model
        second-guessing the first (DECISIONS.md 2026-06-16). A non-clean block stays
        DRAFT with its findings attached for review."""
        block = self.draft(model, findings, house_style)
        report = reviewer.review(model, block)  # fidelity_only gate; surfaced, not applied
        return block, report


_SECTION2_BRIEF = """\
The Deal Summary is the paper's first judgement section — it must show the firm
understood the deal before any number.

0. LEAD WITH THE LOAN. Open the section with a tight summary of what the deal
   actually IS — the transaction at a glance — BEFORE any detail on the sponsor or
   borrower. In a few sentences (or a compact summary table): the facility and what
   it funds, the borrower/security at a high level, the headline structure (amount,
   term, rate where on file), the asset and what is being built/bought, and how the
   facility repays. A reader should grasp the shape of the lend from the first
   paragraph. THEN proceed to the detail below. Do not open on the sponsor.
1. Understand the project before any figure. State plainly what is being built or
   bought, why the borrower needs the facility, and how the deal is structured.
   Form a genuine view on whether the project makes commercial sense in its own
   right — the product, the location, the buyer. A project can fail this test
   before a single number is run; do not let figures stand in for judgement.
2. Test the borrower, not the brochure. Where track-record facts are available,
   weigh whether the sponsor has actually delivered work of this scope and type.
   On construction, do not confuse the developer with the builder — scrutinise the
   builder, and especially their cash position (builders fund trades in advance; a
   replacement on a part-built project is expensive and rarely takes warranty risk).
3. Exit as a mechanism, not a label. State what repays the facility, by what path,
   by when — and check it is consistent with the term, the funding table, and any
   presales. "Sale or refinance" with no deal-specific substance is not an exit.
4. Concise and defensible. Choose the facts fundamental to the recommendation and
   state them clearly; if it is not in the paper it is treated as not done. Do not
   make or imply the credit recommendation — that is the human-authored Conclusion.
"""


class Section2DealSummary(LLMSectionAgent):
    section = 2
    mode = AgentMode.JUDGEMENT
    title = "Deal Summary"
    brief = _SECTION2_BRIEF
    task = (
        "Draft the Deal Summary. OPEN with a tight loan summary — what the deal is at "
        "a glance (the facility and what it funds, borrower/security high-level, the "
        "headline structure, the asset/what is being built, and the repayment path) — "
        "before any sponsor detail; a compact summary table is appropriate here. THEN "
        "cover: the sponsor; the project scope; the builder / site manager; current "
        "project status; and the exit strategy. Show that the firm understood the deal "
        "before any numbers, and give a reasoned view on whether the project makes "
        "commercial sense — grounded only in the available facts. Keep it tight."
    )


class Section4ZoningLocation(LLMSectionAgent):
    section = 4
    mode = AgentMode.HYBRID
    title = "Zoning & Location"
    brief = (
        "Establish where the security is and whether the location supports the deal. "
        "Cover the site (address, area, title reference), the zoning and planning "
        "controls, and the development approval(s) and their status/lapse. Relate "
        "location and zoning to viability — is this product right for this area? "
        "(A product mismatched to its locality can fail before a number is run.) "
        "Explain what the zoning MEANS for the project, not just its label. "
        "BRIGHT LINE — INTERPRET, DO NOT IMPORT. You MAY reason about the FIT between "
        "the RECORDED zoning and the RECORDED project (e.g. 'two dwellings on land "
        "recorded as R2 Low Density is consistent with that zoning'). You may NOT "
        "state external knowledge that is not on file: the specific uses a zone "
        "permits or prohibits, market conditions (supply, demand, vacancy, price "
        "trends), the site's tenure/use history, or the legal effect of a planning "
        "instrument (e.g. what a construction certificate legally establishes). Where "
        "such detail would strengthen the section, SCAFFOLD it (sourced from the "
        "valuation / planning report), never assert it. Attribute the sponsor's market "
        "view ('demand and limited supply') explicitly to the sponsor, once. Hedge "
        "anything forward-looking (construction 'would', not 'will', lift value). This "
        "section is normally drafted from the valuation report, which covers site, "
        "location and zoning in detail; where that detail (title reference and "
        "encumbrances, site characteristics — frontage, shape, access, flooding, "
        "easements — DA conditions, environmental/geotechnical findings) is not on "
        "file, SCAFFOLD those as best-practice subsections for completion. Factual "
        "where the facts are factual; a measured view where location or zoning bears "
        "on the credit. No padding."
    )
    task = (
        "Draft Zoning & Location: site and title, zoning and planning controls (and "
        "what they mean for the project), the development approval and its currency, "
        "environmental/geotechnical matters, and a view on how the location and "
        "permitted use support the project. Scaffold missing material elements for "
        "completion. Grounded only in the available facts."
    )


class Section5BorrowerAnalysis(LLMSectionAgent):
    section = 5
    mode = AgentMode.JUDGEMENT
    title = "Borrower Analysis"
    brief = (
        "The borrower is who repays the firm. Test the track record, not the website: "
        "where claims exist, weigh whether the sponsor has actually delivered work of "
        "this scope and type. Set out the ownership / trust structure and who stands "
        "behind the debt (guarantees, and any limits). Assess the borrowing entity's "
        "financial position and capacity to carry the debt, and what security is "
        "available if the package needs enhancing. On construction, scrutinise the "
        "builder separately — track record, financials, and especially cash. State "
        "capacity as a finding, not a flattering summary. Where track record, "
        "structure, financial position, or builder detail is not on file, SCAFFOLD "
        "the best-practice structure for the analyst to complete (e.g. sponsor "
        "personal and professional experience, prior comparable projects, capability "
        "and character; the trust deed and trustee power to borrow/grant security; "
        "the builder's cash position) rather than dismissing the gap in a sentence."
    )
    task = (
        "Draft Borrower Analysis: the sponsor and track record (tested, not "
        "transcribed), the ownership/guarantee structure, the sponsor's financial "
        "position and capacity, and — for construction — the builder. Scaffold any "
        "missing material element for completion. Grounded only in the available facts."
    )


class Section6ValuationAnalysis(LLMSectionAgent):
    section = 6
    mode = AgentMode.ANALYTICAL
    title = "Valuation Analysis"
    brief = (
        "Do not transcribe the valuation — understand and test it. Document the "
        "valuer's figures (bases, methodology, and comparables where available) as "
        "the baseline, then layer the firm's own reasonability over them. Apply "
        "professional scepticism: valuations are usually borrower-arranged even when "
        "reassigned to the firm, so test rather than accept. Compute and state the "
        "LVR against the relevant basis and the covenant ceiling. Where the "
        "comparable evidence or methodology inputs are NOT in the available facts, "
        "state that the firm's independent comparable testing is outstanding rather "
        "than implying it was done. The valuation is a material input, not the credit "
        "judgement; attribute the valuer's conclusions to the valuer."
    )
    task = (
        "Draft Valuation Analysis: the valuer, date and basis; the values by basis; "
        "the methodology and any comparable evidence available; the LVR against the "
        "as-if-complete value and the covenant; and the firm's reasonability view. "
        "Where comp evidence is not in the facts, mark independent testing outstanding."
    )


class Section7RisksMitigations(LLMSectionAgent):
    section = 7
    mode = AgentMode.JUDGEMENT
    title = "Risks & Mitigations"
    brief = (
        "Identify the risks that genuinely bear on THIS deal — not a generic laundry "
        "list — drawn from the whole thesis (project, borrower, builder, valuation, "
        "exit). Pair each risk with a concrete, specific mitigant: a legal "
        "protection, a covenant, a structural feature, equity already spent, or a "
        "lever the firm can use. A mitigant must ACTUALLY reduce the risk and must be "
        "tested for sufficiency ('so what — does this hold the risk to an acceptable "
        "level?'). A fact that raises a concern is NOT a mitigant: where a risk has "
        "no genuine mitigant on file (e.g. no builder track record beyond a licence, "
        "no insurance confirmed), say so as an unmitigated RESIDUAL that ELEVATES the "
        "risk — never dress a concern up as a mitigant, and assess such risks higher. "
        "Sponsor reputation, character, and track record are themselves risk lines on "
        "a construction deal. Any flagged registry finding must appear here paired "
        "with its mitigant or disclosed as residual. Precision over length — the "
        "section earns its weight through specificity, not volume."
    )
    task = (
        "Draft Risks & Mitigations: the specific risks bearing on this deal, each "
        "with a concrete mitigant grounded in the available facts. Cover at least "
        "exit, execution/builder, and valuation/LVR risk where relevant. No generic "
        "risks."
    )
    # Structured rows for a Risk / Assessment / Mitigant table.
    format_note = (
        "Output each material risk as a block of exactly three lines:\n"
        "'## <short risk name>'\n'Assessment: <Low|Medium|High>'\n"
        "'Mitigant: <one concrete, specific mitigant grounded in the available facts>'\n"
        "Separate blocks with a blank line. No other prose, no preamble."
    )


class Section8OtherConsiderations(LLMSectionAgent):
    section = 8
    mode = AgentMode.HYBRID
    title = "Other Considerations"
    brief = (
        "Complete the credit picture: the security structure (mortgage, guarantees, "
        "general security), the proposed covenants and what risk each manages, the "
        "AML/KYC and screening conclusions (results only — conclusions, never raw "
        "identity data; the provider belongs in provenance), insurance, and "
        "environmental considerations. State conclusions and attribute external ones "
        "(legal, screening). Disclose any flagged screening or ATO findings plainly "
        "rather than smoothing them over. Where the security package, screening "
        "conclusions, insurance, or environmental position are not on file, SCAFFOLD "
        "the best-practice structure for the analyst rather than null-stating. A "
        "material absence that bears directly on credit risk (e.g. unconfirmed "
        "insurance) is also a risk for Section 7 — note it as such, do not let it sit "
        "only here."
    )
    task = (
        "Draft Other Considerations: security structure, covenants (and the risk each "
        "manages), AML/KYC/screening conclusions, insurance, and environmental "
        "matters. Disclose flagged findings. Grounded only in the available facts."
    )


class Section9Conclusion(LLMSectionAgent):
    section = 9
    mode = AgentMode.SCAFFOLD
    title = "Conclusion"
    brief = (
        "Synthesise the credit thesis: draw together why the deal does or does not "
        "hang together — the key supports and the principal risks already established "
        "in the paper — into a clear scaffold. STOP before the recommendation: the "
        "credit recommendation, approval, or decline is authored by the human analyst "
        "(D8) and must NOT be written by the agent. End at the point where the "
        "analyst's recommendation belongs."
    )
    task = (
        "Write the Conclusion scaffold: a concise synthesis of the thesis — the key "
        "factors supporting the credit and the principal risks or conditions — drawn "
        "only from the established facts. Do NOT state a recommendation, approval, or "
        "decline; leave that to the analyst."
    )


# Section number -> agent class for the prose sections (1 and 3 are deterministic tables).
PROSE_AGENTS: dict[int, type[LLMSectionAgent]] = {
    2: Section2DealSummary,
    4: Section4ZoningLocation,
    5: Section5BorrowerAnalysis,
    6: Section6ValuationAnalysis,
    7: Section7RisksMitigations,
    8: Section8OtherConsiderations,
    9: Section9Conclusion,
}
