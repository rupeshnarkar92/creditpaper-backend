"""House Style Profile (D10): structure, voice, and boilerplate learned from a
client's prior approved papers. Calibrates HOW the paper reads and is organised —
never WHAT it claims (deal facts come only from the model; website copy never
calibrates prose, D10).

Phase 0: built by analysing fixtures/prior_papers/*. This module defines the
profile shape and seeds the canonical 3Capital section taxonomy observed across
the 15 prior papers. Voice and boilerplate fields are populated as the analysis
lands; `source_papers` records which papers the profile was learned from.
"""

from __future__ import annotations

from pydantic import BaseModel

from creditpaper.sections import SECTION_MODES, AgentMode


class SectionStyle(BaseModel):
    section: int  # aligns to SECTION_MODES / the section agents
    key: str
    title: str  # the client's heading wording
    typical_subsections: list[str] = []
    notes: str = ""  # voice/structure guidance for the section agent


class HouseStyleProfile(BaseModel):
    client: str
    # 3Capital's standard title is "Internal Credit Memorandum" (confirmed by Jason
    # 2026-06-15); a recent paper used "Internal Credit Paper". Per client/era.
    paper_title: str = "Internal Credit Memorandum"
    sections: list[SectionStyle] = []
    voice: dict[str, str] = {}  # tone attributes: register, person, hedging, sentence length...
    boilerplate: dict[str, str] = {}  # recurring blocks: disclaimer, why_3capital, contact
    # Real prior-deal sections used as FORM/DEPTH exemplars (not content to copy);
    # tenant prose — populated at runtime from gitignored prior papers, never committed.
    section_exemplars: dict[int, str] = {}
    # Per-section "house playbook" distilled from ALL prior papers (principles and
    # patterns, not deal facts) — learned via harness/learn_house_style.py (D10).
    section_playbooks: dict[int, str] = {}
    source_papers: list[str] = []  # filenames analysed — provenance of the profile


# Canonical 3Capital section taxonomy (Phase-0 seed from the 15 prior papers;
# matches the Riverton Rise build deal). Section numbers align to SECTION_MODES.
CANONICAL_3CAPITAL_SECTIONS: list[SectionStyle] = [
    SectionStyle(section=1, key="deal_snapshot", title="DEAL SNAPSHOT"),
    SectionStyle(
        section=2,
        key="deal_summary",
        title="DEAL SUMMARY",
        typical_subsections=[
            "Funding Overview",
            "Project Sponsors",
            "Project Manager",
            "Project Status",
            "Quantity Surveyor",
            "Exit Strategy",
        ],
    ),
    SectionStyle(
        section=3, key="funding_snapshot", title="FUNDING SNAPSHOT", typical_subsections=["Funding Table"]
    ),
    SectionStyle(
        section=4,
        key="zoning_location",
        title="ZONING & LOCATION",
        typical_subsections=["Location & Size", "Site History and Approvals", "Planning"],
    ),
    SectionStyle(
        section=5,
        key="borrower_analysis",
        title="BORROWER ANALYSIS",
        typical_subsections=[
            "Guarantors / Project Sponsors",
            "Ownership Structure",
            "Sponsor Financial Position",
        ],
    ),
    SectionStyle(
        section=6,
        key="valuation_analysis",
        title="VALUATION ANALYSIS",
        typical_subsections=[
            "Project Description",
            "Market Overview",
            "'As Is' Market Value",
            "'As If Complete' Market Value",
        ],
    ),
    SectionStyle(
        section=7, key="risks_mitigations", title="RISKS & MITIGATIONS", typical_subsections=["Key Risks"]
    ),
    SectionStyle(
        section=8,
        key="other_considerations",
        title="OTHER CONSIDERATIONS",
        typical_subsections=[
            "Security Structure",
            "Proposed Covenants",
            "AML & KYC Conclusions",
            "Environmental Considerations",
        ],
    ),
    SectionStyle(
        section=9,
        key="conclusion",
        title="CONCLUSION",
        notes="Thesis scaffold only; recommendation is human-authored (D8). "
        "Followed by a CREDIT APPROVAL signature block (committee members).",
    ),
]

# Recurring non-analytical blocks seen across papers (front/back matter), to be
# captured verbatim-ish per client: "WHY 3 CAPITAL", "DISCLAIMER", "LET'S GET IN
# TOUCH" / contact. These are boilerplate, not generated prose.
KNOWN_BOILERPLATE_KEYS = ["why_3capital", "disclaimer", "contact"]


def seed_3capital_profile() -> HouseStyleProfile:
    """Starting profile for 3Capital — structure seeded, voice/boilerplate TBD
    from the prior-paper analysis (Phase 0)."""
    return HouseStyleProfile(
        client="3Capital",
        paper_title="Internal Credit Memorandum",
        sections=list(CANONICAL_3CAPITAL_SECTIONS),
        # High-level voice defaults (generic style attrs; deeper voice from the
        # prior-paper analysis is layered in at runtime). These are not tenant prose.
        voice={
            "register": "institutional credit-committee; third person",
            "tense": "present for current state, past for completed diligence",
            "sentences": "plain, declarative; precision over length",
            "tone": "findings, not enthusiasm; no marketing language",
        },
    )


assert set(SECTION_MODES) == {s.section for s in CANONICAL_3CAPITAL_SECTIONS}, (
    "section taxonomy must cover exactly the nine section agents"
)
assert SECTION_MODES[9] == AgentMode.SCAFFOLD  # D8: conclusion is a scaffold
