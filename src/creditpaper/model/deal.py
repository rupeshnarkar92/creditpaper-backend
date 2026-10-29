"""Top-level deal model, schema v0.10 (Build Plan Â§3, FIELD_DICTIONARY.md).

FROZEN as the Wave-1 contract (DECISIONS.md 2026-06-11): the five instances
share nothing but this schema, so paths and shapes here, the field dictionary,
and the instance briefs agree exactly. Changes go through DECISIONS.md, never
improvised per-instance.

Shape rules: every fact is a Fact (value + provenance + confidence + status);
collections are keyed dicts of Facts or typed blocks so each item carries its
own provenance anchor (D4 â€” a single anchor on a whole list is not provenance).
Writes go through model/apply.py only. Versioned; per-section regeneration
operates on snapshots.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from enum import Enum

from creditpaper.model.core import DealBase, Fact, ProseBlock


class DealType(str, Enum):
    CONSTRUCTION = "construction"
    BRIDGE = "bridge"
    RESIDUAL_STOCK = "residual_stock"


class DeliveryModel(str, Enum):
    """How the construction is delivered — drives the EXTENT and shape of builder DD
    (memory: construction-delivery-model-drives-builder-dd). DERIVED from artifacts
    (is there a building contract? is the builder a related group entity? is there a
    tripartite/step-in deed?), NOT assumed from whether the builder is the sponsor."""

    OWNER_BUILDER = "owner_builder"  # sponsor builds under own licence; no contract
    RELATED_PARTY_BUILDER = "related_party_builder"  # separate building co, same UBO; contract + tripartite
    INDEPENDENT_BUILDER = "independent_builder"  # arm's-length builder; full builder DD


class DealIdentity(DealBase):
    """Identity, type, key dates (`deal.*`). `expected_funding_date` is
    HUMAN_ENTRY, collected at deal creation so the valuation slot's currency
    check (#5) can run on the very first upload."""

    name: Fact[str] = Fact()
    type: Fact[DealType] = Fact()
    # Construction delivery model — DERIVED (model/derive.py) from artifacts; drives
    # builder-DD extent, the material-document check, and §2/§7 framing. Owner-builder
    # folds builder DD into the sponsor; related-party adds non-arm's-length pricing
    # risk but a tripartite step-in mitigant. (DECISIONS.md 2026-06-18)
    delivery_model: Fact[DeliveryModel] = Fact()
    expected_funding_date: Fact[datetime.date] = Fact()
    # Exit thesis: narrative, analyst-entered (not derivable) — what repays the
    # facility, by what path, by when. Feeds Section 2 (DECISIONS.md 2026-06-15).
    exit_strategy: Fact[str] = Fact()


# ---------------------------------------------------------------------------
# Economics + ITS terms (Instance A)
# ---------------------------------------------------------------------------


class FeeLine(DealBase):
    """Every fee is shown in both % and $ terms in the paper."""

    label: str = ""  # e.g. "Establishment fee"
    pct: Fact[Decimal] = Fact()  # % of facility
    amount: Fact[Decimal] = Fact()  # dollars


class FundingLine(DealBase):
    """One row of the funding table as a total/senior/equity triple. The identity
    is total = senior + equity (DECISIONS.md 2026-06-15): any TWO are anchored to
    source docs and the THIRD is DERIVED (model/derive.py). Equity is never
    asserted — it falls out of what the asset is worth (total) and how much we
    lend against it (senior). The engine derives whichever column is missing."""

    label: str = ""  # as stated in the source document
    total: Fact[Decimal] = Fact()  # what the line is worth / costs
    senior: Fact[Decimal] = Fact()  # how much we lend against it
    equity: Fact[Decimal] = Fact()  # residual; typically DERIVED = total - senior


class FacilityEconomics(DealBase):
    """Extracted from the signed ITS (DECISIONS.md 2026-06-10, amends D5).

    Deliberately paper-simple: the funding table and fees in % and $ terms — the
    commercial feasibility of the loan. No xlsx pricing models are read. The
    funding table is a total/senior/equity matrix (anchor two, derive the third,
    DECISIONS.md 2026-06-15); aggregates are DERIVED across it.
    """

    facility_amount: Fact[Decimal] = Fact()
    interest_rate: Fact[Decimal] = Fact()  # % p.a.
    term_months: Fact[int] = Fact()
    fees: dict[str, FeeLine] = {}  # keyed: establishment, line, ...
    funding_table: dict[str, FundingLine] = {}  # keyed rows; anchor two columns, derive the third
    # Schema 0.11. Not stated = MISSING, so "unknown" is never a guessed value.
    limit_includes_capitalised_interest: Fact[bool] = Fact()
    contingency: Fact[Decimal] = Fact()  # construction contingency allowed in the budget
    # Aggregates — DERIVED across the funding table (model/derive.py):
    total_cost: Fact[Decimal] = Fact()  # sum of line totals
    senior_total: Fact[Decimal] = Fact()  # sum of line seniors; cross-checks to facility_amount
    equity_total: Fact[Decimal] = Fact()  # sum of line equities = total_cost - senior_total


class Covenant(DealBase):
    """A key covenant used/proposed to manage the risk of the lend
    (DECISIONS.md 2026-06-10: a list, not a design workspace)."""

    name: Fact[str] = Fact()
    summary: Fact[str] = Fact()
    threshold: Fact[str] = Fact()  # as worded in the ITS
    threshold_value: Fact[Decimal] = Fact()  # numeric form for check logic (e.g. 45 for 45%)
    basis: Fact[str] = Fact()  # schema 0.11: what the threshold is measured against, as worded


class DerivedMetrics(DealBase):
    """Covenant and security sensitivities computed ONCE from anchored facts
    (model/derive.py), homed here so every section CITES one canonical value
    instead of recomputing it in prose — independent per-section recomputation
    drifted (LVR headroom rendered three different ways in review round 1,
    2026-06-16). DERIVED provenance records the formula and the input paths (D4),
    and a check can re-run them, exactly like the funding aggregates. Added with
    schema v0.7 (DECISIONS.md 2026-06-16)."""

    # LVR basis rule (DECISIONS.md 2026-08-21): DAY ONE is always taken on the
    # as-is valuation; the REMAINDER of the loan is taken on the as-if-complete
    # value or the project-related site value, selected by the loan. `lvr_basis`
    # records which ongoing basis was selected, so the paper can never state an
    # LVR without naming what it is a percentage of.
    lvr_basis: Fact[str] = Fact()  # ongoing denominator key: as_if_complete | prsv | …
    lvr_pct: Fact[Decimal] = Fact()  # facility / <lvr_basis> * 100
    lvr_ceiling_value: Fact[Decimal] = Fact()  # threshold% * <lvr_basis>
    lvr_headroom_amount: Fact[Decimal] = Fact()  # ceiling_value - facility (facility $ to breach)
    lvr_breach_value: Fact[Decimal] = Fact()  # <lvr_basis> value at which LVR == threshold
    lvr_breach_drop_pct: Fact[Decimal] = Fact()  # % fall in <lvr_basis> to breach
    lcr_pct: Fact[Decimal] = Fact()  # facility / total_cost * 100
    lcr_ceiling_amount: Fact[Decimal] = Fact()  # threshold% * total_cost
    lcr_headroom_amount: Fact[Decimal] = Fact()  # ceiling_amount - facility (loan $ to breach)
    # The day-one pair. NOT facility/as_is: the numerator is the DAY-ONE ADVANCE,
    # because a construction facility draws progressively and only the land line
    # is out at close. The earlier facility-based definition overstated the
    # day-one position and was replaced 2026-06-16 (`derive.py::_day_one_advance`).
    asis_lvr_pct: Fact[Decimal] = Fact()  # day_one_advance / as_is * 100
    asis_security_gap: Fact[Decimal] = Fact()  # day_one_advance - as_is (positive = deficit)


class ThesisSpine(DealBase):
    """The deal's narrative spine — the firm's reasoned read, produced ONCE by a
    fast pre-pass and inherited by every section so they tell one coherent story
    (parallel generation, Phase A). The qualitative analogue of `metrics`: sections
    coordinate through this shared artifact, not by reading each other's prose (D1).
    It is synthesised framing grounded in the facts (like analysis) — not a citable
    deal fact, so it carries no provenance. Schema v0.8 (DECISIONS.md 2026-06-16)."""

    overall_read: str = ""  # the one-paragraph credit thesis: does it hang together, why
    dominant_risks: list[str] = []  # the 1–3 risks that drive the decision, ranked
    key_tensions: list[str] = []  # cross-reads that create tension (e.g. covenants at ceiling vs exit)
    # Canonical framing of items several sections touch, so they don't each reframe
    # them differently (e.g. exit, covenants, security, valuation currency).
    canonical_framings: dict[str, str] = {}
    present: bool = False  # True once the pre-pass has run

    def as_brief(self) -> str:
        """Compact text injected into each section agent."""
        if not self.present:
            return ""
        risks = "; ".join(self.dominant_risks) or "—"
        tensions = "; ".join(self.key_tensions) or "—"
        framings = "\n".join(f"  - {k}: {v}" for k, v in self.canonical_framings.items())
        return (
            "DEAL THESIS (the firm's shared read — keep every section consistent with "
            "this spine; do not re-frame these items differently):\n"
            f"Overall read: {self.overall_read}\n"
            f"Dominant risks (ranked): {risks}\n"
            f"Key tensions: {tensions}\n"
            f"Canonical framings:\n{framings}"
        )


class ItsTerms(DealBase):
    """Non-economic ITS terms. Priced terms live in economics.* (same source,
    no duplication); covenants are top-level `covenants` (paths mirror the paper).
    CPs and guarantors are keyed so each carries its own page/clause anchor and
    feeds the outstanding-items register individually (Instance A)."""

    borrower_entity: Fact[str] = Fact()
    guarantors: dict[str, Fact[str]] = {}  # its.guarantors[<key>], name as written
    cp_list: dict[str, Fact[str]] = {}  # its.cp_list[<key>], each CP as stated
    signed: Fact[bool] = Fact()
    signed_date: Fact[datetime.date] = Fact()


# ---------------------------------------------------------------------------
# Valuation (Instance B)
# ---------------------------------------------------------------------------


class ComparableAttributes(DealBase):
    """Common attribute schema: subject + every comparable, valuer's and firm's,
    so Section 6 can benchmark like-for-like (Instance B). Attributes the report
    does not state stay `missing`, never inferred."""

    land_area: Fact[Decimal] = Fact()  # sqm
    gfa_nla: Fact[Decimal] = Fact()  # sqm, whichever measure the report uses
    unit_mix: Fact[str] = Fact()  # beds/baths or mix, where relevant
    location: Fact[str] = Fact()
    distance_from_subject: Fact[str] = Fact()
    sale_date: Fact[datetime.date] = Fact()
    sale_price: Fact[Decimal] = Fact()
    derived_rate: Fact[Decimal] = Fact()  # $/sqm of the relevant measure
    condition: Fact[str] = Fact()
    zoning: Fact[str] = Fact()
    adjustment: Fact[str] = Fact()  # the valuer's stated adjustment
    adjustment_reason: Fact[str] = Fact()


class ValuationMethodology(DealBase):
    approach: Fact[str] = Fact()  # direct comparison / capitalisation / hypothetical development
    # Every input the valuer used (rates psm, cap rate, GRV, costs, profit & risk):
    # sufficient to re-run the valuer's arithmetic in code (Section 6 contract).
    figures: dict[str, Fact[Decimal]] = {}
    adopted_rate: Fact[Decimal] = Fact()  # schema 0.12: $/sqm the valuer adopts (area x rate = value)


class UnitValue(DealBase):
    """The valuer's assessed value for one subject unit (schema 0.12). Not comparable
    evidence: a comparable is a sale and carries a date; this is the subject itself."""

    unit_mix: Fact[str] = Fact()  # beds/baths as stated, e.g. "3 + S"
    area: Fact[Decimal] = Fact()  # internal area, sqm
    outdoor_area: Fact[Decimal] = Fact()  # private open space, sqm
    value: Fact[Decimal] = Fact()  # assessed value
    rate: Fact[Decimal] = Fact()  # $/sqm of the internal area


class PropertyDetails(DealBase):
    """`valuation.property.*` â€” feeds Section 4."""

    address: Fact[str] = Fact()
    title_reference: Fact[str] = Fact()
    zoning: Fact[str] = Fact()
    planning_controls: Fact[str] = Fact()
    site_area: Fact[Decimal] = Fact()  # sqm
    gfa_total: Fact[Decimal] = Fact()  # total Gross Floor Area, sqm


class MarketCommentary(DealBase):
    """The report's market commentary, restated in its own terms â€” feeds
    Sections 2, 6 and 7. List-valued commentary is keyed per item (D4)."""

    conditions: Fact[str] = Fact()
    supply_demand: Fact[str] = Fact()
    macro_factors: dict[str, Fact[str]] = {}
    locality_outlook: Fact[str] = Fact()
    market_risks: dict[str, Fact[str]] = {}


class Valuation(DealBase):
    """The valuation the paper relies on. Additional reports (e.g. check
    valuations) land keyed in top-level `check_valuations` â€” multi-file slots
    produce keyed field sets, never merged (Instance B)."""

    valuer: Fact[str] = Fact()  # firm + signing valuer
    valuation_date: Fact[datetime.date] = Fact()  # as at which the property is valued
    report_date: Fact[datetime.date] = Fact()  # schema 0.13: when the report was issued
    relying_party: Fact[str] = Fact()  # who may rely; reassignment noted
    expected_funding_date_check: Fact[bool] = Fact()  # feeds check #5 (currency)
    values_by_basis: dict[str, Fact[Decimal]] = {}  # OPEN keyed set: as_is, as_if_complete, prsv...
    methodology: ValuationMethodology = ValuationMethodology()
    comp_set: dict[str, ComparableAttributes] = {}  # every comparable; none invented, none dropped
    subject_attributes: ComparableAttributes = ComparableAttributes()
    # The comp section's summary table verbatim (columns + rows preserved) so the
    # renderer can restate it in the paper; prose detail enriches comp_set entries.
    comp_summary_table: Fact[dict] = Fact()
    property: PropertyDetails = PropertyDetails()
    property_risks: dict[str, Fact[str]] = {}  # valuer-identified; Section 7 candidates
    market: MarketCommentary = MarketCommentary()
    gst_basis: Fact[str] = Fact()  # schema 0.11: the basis the report states its values on
    unit_schedule: dict[str, UnitValue] = {}  # schema 0.12: assessed value per subject unit


# ---------------------------------------------------------------------------
# Security, screening, sponsor cluster (Instances C and E)
# ---------------------------------------------------------------------------


class SecurityProperty(DealBase):
    """Title / lawyer report. Wave 1 owner: Instance E."""

    title_status: Fact[str] = Fact()
    encumbrances: dict[str, Fact[str]] = {}  # each with its own anchor
    ppsr_findings: dict[str, Fact[str]] = {}
    first_mortgage_available: Fact[bool] = Fact()
    lien_position: Fact[int] = Fact()  # schema 0.11: rank on title, 1 = first; prior charges in encumbrances


class CreditScreening(DealBase):
    adverse_findings: dict[str, Fact[str]] = {}  # defaults, judgments, court actions


class AmlScreening(DealBase):
    provider: Fact[str] = Fact()  # the AML/sanctions screening provider (conclusions only, D7)
    sanctions: Fact[bool] = Fact()
    pep: Fact[bool] = Fact()
    adverse_media: Fact[bool] = Fact()


class IdentityScreening(DealBase):
    verified: Fact[bool] = Fact()


class Screening(DealBase):
    """Provider-agnostic results (DECISIONS.md 2026-06-10); provider in
    provenance. Conclusions only, never raw IDs (D7). Wave 1 owner: Instance E."""

    credit: CreditScreening = CreditScreening()
    aml: AmlScreening = AmlScreening()
    identity: IdentityScreening = IdentityScreening()


class SponsorFinancialYear(DealBase):
    """One financial year (`sponsor.financials[<fy>]`). Wave 1 owner: Instance C."""

    revenue_trend: Fact[str] = Fact()
    profitability: Fact[Decimal] = Fact()
    debt_levels: Fact[Decimal] = Fact()
    going_concern: Fact[bool] = Fact()  # 3.11: confirm going concern
    contingent_liabilities: dict[str, Fact[str]] = {}  # all surfaced, none summarised away
    related_party_transactions: dict[str, Fact[str]] = {}


class SponsorEntity(DealBase):
    """One node of the entity graph. Trusts and non-company entities are
    explicitly typed â€” screening unwraps companies but not trusts (3.11)."""

    name: Fact[str] = Fact()  # raw string, no normalisation (check #12)
    entity_type: Fact[str] = Fact()  # company / trust / individual / partnership
    role: Fact[str] = Fact()  # borrower / guarantor / trustee / holdco ...


class SponsorStructure(DealBase):
    """`sponsor.structure.*`. Wave 1 owner: Instance C."""

    entities: dict[str, SponsorEntity] = {}
    ubos: dict[str, Fact[str]] = {}  # ultimate beneficial owners, as identified
    trusts: dict[str, SponsorEntity] = {}  # kept explicit (3.11)
    related_party_flags: dict[str, Fact[str]] = {}


class TrackRecordClaim(DealBase):
    """A discrete, testable track-record claim â€” tested, not transcribed
    (7.3 #2). Wave 1 owner: Instance C."""

    claim: Fact[str] = Fact()
    project: Fact[str] = Fact()
    asset_type: Fact[str] = Fact()
    scale: Fact[str] = Fact()
    year: Fact[int] = Fact()
    verifiable_detail: Fact[str] = Fact()


class Sponsor(DealBase):
    financials: dict[str, SponsorFinancialYear] = {}  # keyed by financial year
    structure: SponsorStructure = SponsorStructure()
    claims: dict[str, TrackRecordClaim] = {}
    # Financial-position facts feeding Section 5 (skin in the game):
    cash_equity_to_date: Fact[Decimal] = Fact()  # cumulative cash equity contributed
    liquidity_cushion: Fact[Decimal] = Fact()  # unused cash buffer in the borrowing entity


class SopAsset(DealBase):
    value: Fact[Decimal] = Fact()
    encumbered_to: Fact[str] = Fact()  # lender named where encumbered; "unencumbered" otherwise


class SopLiability(DealBase):
    amount: Fact[Decimal] = Fact()
    lender: Fact[str] = Fact()


class SopStatement(DealBase):
    """Per guarantor (`sop[<guarantor>]`). Read as a risk mitigation, not a
    tick-box (3.11): the encumbrance mapping answers "who else have they
    borrowed from". Wave 1 owner: Instance C."""

    accountant_prepared: Fact[bool] = Fact()
    assets: dict[str, SopAsset] = {}
    liabilities: dict[str, SopLiability] = {}
    free_cash: Fact[Decimal] = Fact()
    net_position: Fact[Decimal] = Fact()


class AtoPosition(DealBase):
    """The ATO can become a competing creditor in default (3.11). Anything
    non-clear is itemised and `flagged` â€” and check #17 then forces the paper
    to disclose it. Wave 1 owner: Instance C."""

    outstanding_debts: dict[str, Fact[str]] = {}
    payment_arrangements: dict[str, Fact[str]] = {}
    disputes: dict[str, Fact[str]] = {}
    position_clear: Fact[bool] = Fact()


# ---------------------------------------------------------------------------
# Construction cluster (Instance D)
# ---------------------------------------------------------------------------


class BuilderLicence(DealBase):
    number: Fact[str] = Fact()
    current: Fact[bool] = Fact()
    category: Fact[str] = Fact()  # category-match feeds check #14


class BuilderFinancials(DealBase):
    cash_position: Fact[Decimal] = Fact()  # 7.3 #2: scrutinise the builder, especially cash
    defaults: dict[str, Fact[str]] = {}


class Builder(DealBase):
    licence: BuilderLicence = BuilderLicence()
    financials: BuilderFinancials = BuilderFinancials()
    references: dict[str, Fact[str]] = {}  # structured entries
    site_manager: Fact[str] = Fact()  # engaged site manager / supervisor (esp. owner-builder deals)


class QsDivergence(DealBase):
    """Both figures, so checks #3/#4 can compute materiality."""

    qs_figure: Fact[Decimal] = Fact()
    developer_figure: Fact[Decimal] = Fact()


class QsCostLine(DealBase):
    """One line of the QS cost breakdown, keyed by a canonical category that aligns
    with `economics.funding_table` keys so check #3 can pair them line-by-line (the
    line-vs-line reconciliation Jason asked for, 2026-06-17).

    `basis` is what the amount is measured on, and it is required for the pairing to
    mean anything. A QS report typically prints several columns — total development
    cost, costs already incurred, cost to complete — and the funding table's `.total`
    is TOTAL cost. Pairing a cost-to-complete figure against it compares two
    different quantities and reports the difference as a divergence.

    That is not hypothetical: on 2026-08-13 the check reported three divergences
    that were entirely artefacts of this. The QS construction figure equalled the
    funding table's SENIOR cell to the cent, because the senior line is sized to
    fund exactly the remaining construction cost — so QS-cost-to-complete and
    funding-senior are the same number by construction, and the check was comparing
    it to total cost. Professional fees and statutory contributions showed the same
    signature: nearly all spent before the facility draws, so remaining is near zero
    while total is large.

    The contingency catch was unaffected and is real, which is the tell: contingency
    is by definition unspent, so its cost-to-complete equals its total.
    """

    amount: Fact[Decimal] = Fact()
    label: Fact[str] = Fact()  # the QS's own wording for the line
    basis: Fact[str] = Fact()  # total_cost | cost_to_complete | costs_to_date


class QsFindings(DealBase):
    firm: Fact[str] = Fact()  # the financier's QS (e.g. Mitchell Brandtman)
    project_cost: Fact[Decimal] = Fact()
    cost_to_complete: Fact[Decimal] = Fact()
    contract_sum_view: Fact[str] = Fact()  # the QS's view of the contract sum
    cost_lines: dict[str, QsCostLine] = {}  # the cost-to-complete breakdown (vs funding table)
    overrun_risks: dict[str, Fact[str]] = {}
    items_not_in_feasibility: dict[str, Fact[str]] = {}
    divergence_vs_developer: dict[str, QsDivergence] = {}
    agreement_appropriate: Fact[bool] = Fact()  # 3.11: construction agreement appropriate


class Retention(DealBase):
    amount: Fact[Decimal] = Fact()
    timing: Fact[str] = Fact()


class BuildingContract(DealBase):
    """Confirming-document pattern (DECISIONS.md 2026-06-10): extracted from
    the contract on arrival; lawyers' review confirms or flags via the apply
    layer. Wave 1 owner: Instance D."""

    contract_type: Fact[str] = Fact()  # lump-sum vs cost-plus
    contract_sum: Fact[Decimal] = Fact()
    retention: Retention = Retention()
    defects_liability_period: Fact[str] = Fact()
    programme: Fact[str] = Fact()
    key_clauses: dict[str, Fact[str]] = {}  # review findings the team must form a view on
    # Tripartite / step-in deed — the lender's right to step into the building contract
    # on builder default (a credit mitigant feeding §2/§7; impossible under owner-builder).
    # Often a SEPARATE deed; captured where the contract or a side deed references it. (GL-12)
    tripartite_present: Fact[bool] = Fact()
    step_in_rights: Fact[str] = Fact()


class DaCondition(DealBase):
    text: Fact[str] = Fact()
    status: Fact[str] = Fact()
    blocker: Fact[bool] = Fact()  # classification justified by the condition's own text


class DaContribution(DealBase):
    """A monetary obligation under the approval — developer/infrastructure/open-space
    contribution, levy or bond. The $ is frequently on a SEPARATE notice (QLD
    Infrastructure Charges Notice, VIC DCP/ICP rate, WA formula), so `amount` may be
    missing even where the obligation is stated; `amount_on_separate_notice` records
    that, so it scaffolds as an analyst-confirm rather than reading as $0."""

    description: Fact[str] = Fact()
    amount: Fact[Decimal] = Fact()  # where stated on the approval
    trigger: Fact[str] = Fact()  # when payable (e.g. before build authorisation)
    amount_on_separate_notice: Fact[bool] = Fact()


class DownstreamApproval(DealBase):
    """Build / occupation authorisation — a SEPARATE instrument in every Australian
    jurisdiction (NSW Construction/Occupation Certificate; VIC & WA Building Permit /
    Occupancy Permit; QLD Building Development Approval / Certificate of Occupancy; SA
    Building Consent within the approval / Certificate of Occupancy). Usually NOT in
    the planning approval, so status is typically analyst-confirmed."""

    kind: Fact[str] = Fact()  # what it is, named as the jurisdiction names it
    reference: Fact[str] = Fact()
    status: Fact[str] = Fact()  # issued / not yet issued / unknown


class DevelopmentApproval(DealBase):
    """Jurisdiction-NEUTRAL planning approval (`da.*`). Models the credit CONCEPTS, not
    one state's artifacts — the extractor maps any AU regime (NSW development consent /
    VIC planning permit / QLD decision notice / WA & SA development approval) onto these
    (research + DECISIONS.md 2026-06-16). Off-document facts (currency status,
    build/occupation status, contribution $ on a separate notice) scaffold to
    analyst-confirm and feed check #13."""

    instrument_type: Fact[str] = Fact()  # as stated: development consent / planning permit / ...
    reference: Fact[str] = Fact()  # the approval identifier (DA/permit/application no.)
    authority: Fact[str] = Fact()  # the consent authority / responsible authority
    approved_date: Fact[datetime.date] = Fact()
    approved_description: Fact[str] = Fact()  # what may be built (the approved scheme)
    approved_use: Fact[str] = Fact()
    # Operative & authorises commencement — FALSE for a NSW deferred-commencement
    # consent, VIC Notice of Decision, QLD preliminary approval, or SA planning-consent
    # only. The "is this build-ready?" credit gate.
    build_ready: Fact[bool] = Fact()
    build_ready_basis: Fact[str] = Fact()  # why (the instrument's own words)
    lapse_date: Fact[datetime.date] = Fact()  # where a date is stated
    lapse_basis: Fact[str] = Fact()  # the currency RULE as stated — never assume a period
    approval_current: Fact[bool] = Fact()  # in force / not lapsed (status; often analyst-confirmed)
    conditions: dict[str, DaCondition] = {}
    contributions: dict[str, DaContribution] = {}
    build_authorisation: DownstreamApproval = DownstreamApproval()
    occupation_authorisation: DownstreamApproval = DownstreamApproval()
    plans_match_feasibility: Fact[bool] = Fact()


class EquityInvoice(DealBase):
    supplier: Fact[str] = Fact()
    amount: Fact[Decimal] = Fact()
    date: Fact[datetime.date] = Fact()
    description: Fact[str] = Fact()  # non-project invoices flagged, not counted


class EquityEvidence(DealBase):
    """Equity already spent is a real mitigation; a claim is not (3.11)."""

    invoices: dict[str, EquityInvoice] = {}
    total: Fact[Decimal] = Fact()  # totalled in code; feeds check #1


class PresaleDeposit(DealBase):
    amount: Fact[Decimal] = Fact()
    paid: Fact[bool] = Fact()
    holder: Fact[str] = Fact()
    form: Fact[str] = Fact()  # schema 0.11: cash, deposit bond, bank guarantee, as worded


class Presale(DealBase):
    """Per contract. "Signed but conditional" never counts as qualifying (3.11)."""

    purchaser: Fact[str] = Fact()
    price: Fact[Decimal] = Fact()
    deposit: PresaleDeposit = PresaleDeposit()
    arms_length: Fact[bool] = Fact()
    exchanged: Fact[bool] = Fact()
    unconditional: Fact[bool] = Fact()
    related_party_disclosed: Fact[bool] = Fact()
    sunset_date: Fact[datetime.date] = Fact()  # schema 0.11: the contract's sunset date


class ConsultantReport(DealBase):
    """Geotech / environmental: anything that increases cost-to-complete or
    timing risk and is not priced in the feasibility (3.11). Findings keep the
    report's own severity language; cross-references qs.items_not_in_feasibility."""

    findings: dict[str, Fact[str]] = {}


class Tripartite(DealBase):
    step_in_rights: Fact[bool] = Fact()  # presence/absence stated explicitly
    funding_protection_on_builder_failure: Fact[str] = Fact()
    status: Fact[str] = Fact()  # draft vs executed


# ---------------------------------------------------------------------------
# Small extractors + bridge / residual stock (Instance E)
# ---------------------------------------------------------------------------


class RatesClearance(DealBase):
    """One of three (`rates[land_tax|water|council]`). Checks #11, #12."""

    cleared: Fact[bool] = Fact()  # cleared only if actually cleared
    issue_date: Fact[datetime.date] = Fact()
    property: Fact[str] = Fact()  # raw string
    entity: Fact[str] = Fact()  # raw string


class InsuranceCertificate(DealBase):
    cover_type: Fact[str] = Fact()
    sum: Fact[Decimal] = Fact()
    parties_noted: dict[str, Fact[str]] = {}  # SPV / security trustee detection
    period_start: Fact[datetime.date] = Fact()
    period_end: Fact[datetime.date] = Fact()  # vs loan term (check #7)


class FundingTable(DealBase):
    """The borrower's own funding table, captured as stated — check #2 reconciles
    it against economics.* (discrepancies are the registry's job, not the
    extractor's). Same total/senior/equity matrix shape as economics."""

    facility_amount: Fact[Decimal] = Fact()
    lines: dict[str, FundingLine] = {}  # anchor two columns, derive the third


class Payout(DealBase):
    amount: Fact[Decimal] = Fact()
    mortgagee: Fact[str] = Fact()
    method: Fact[str] = Fact()  # settlement method (3.11)


class RfiItem(DealBase):
    description: Fact[str] = Fact()
    status: Fact[str] = Fact()  # feeds the disclosure list


class RfiRegister(DealBase):
    items: dict[str, RfiItem] = {}


class PurchaseContract(DealBase):
    """Bridge (acquisitions)."""

    vendor: Fact[str] = Fact()
    price: Fact[Decimal] = Fact()
    settlement_date: Fact[datetime.date] = Fact()
    conditions: dict[str, Fact[str]] = {}


class Lease(DealBase):
    tenant: Fact[str] = Fact()
    rent: Fact[Decimal] = Fact()  # p.a.
    term: Fact[str] = Fact()
    expiry: Fact[datetime.date] = Fact()


class DaLodgement(DealBase):
    """Bridge: evidence the DA has been lodged."""

    lodged: Fact[bool] = Fact()
    lodgement_date: Fact[datetime.date] = Fact()
    reference: Fact[str] = Fact()


class OcTitle(DealBase):
    """Residual stock: OC and title registration per lot."""

    lot: Fact[str] = Fact()
    title_reference: Fact[str] = Fact()
    registered: Fact[bool] = Fact()


class PriceList(DealBase):
    approved: Fact[bool] = Fact()
    lots: dict[str, Fact[Decimal]] = {}  # approved price per lot


class ContractEoi(DealBase):
    kind: Fact[str] = Fact()  # exchanged contract vs EOI
    purchaser: Fact[str] = Fact()
    price: Fact[Decimal] = Fact()
    status: Fact[str] = Fact()


class DepositsTrust(DealBase):
    held_in_trust: Fact[bool] = Fact()
    undertaking_present: Fact[bool] = Fact()
    total: Fact[Decimal] = Fact()


class SubordinatedTranche(DealBase):
    """Debt ranking behind the senior facility, from a deed of priority or the
    subordinated facility's own terms (schema 0.11)."""

    lender: Fact[str] = Fact()
    amount: Fact[Decimal] = Fact()  # the subordinated facility limit
    priority_amount: Fact[Decimal] = Fact()  # the senior priority amount the deed fixes
    standstill: Fact[str] = Fact()  # the subordinated lender's standstill, as worded
    maturity: Fact[datetime.date] = Fact()


class CapitalStack(DealBase):
    """Who else is lending against the deal, and where they rank (schema 0.11)."""

    subordinated: dict[str, SubordinatedTranche] = {}


# ---------------------------------------------------------------------------
# The deal model
# ---------------------------------------------------------------------------


class DealModel(DealBase):
    """The product of the pipeline (D1). The renderer is downstream and disposable.

    Writes go through model/apply.py only (DECISIONS.md 2026-06-11)."""

    model_version: int = 1
    schema_version: str = "0.13"
    tenant_id: str
    deal_id: str
    deal: DealIdentity = DealIdentity()
    created_at: datetime.datetime | None = None

    economics: FacilityEconomics = FacilityEconomics()
    its: ItsTerms = ItsTerms()
    covenants: dict[str, Covenant] = {}  # source: signed ITS
    capital_stack: CapitalStack = CapitalStack()  # schema 0.11: subordinated debt and priority
    metrics: DerivedMetrics = DerivedMetrics()  # DERIVED covenant/security sensitivities
    thesis: ThesisSpine = ThesisSpine()  # shared narrative spine (Phase A parallel generation)
    funding_table: FundingTable = FundingTable()  # borrower's view (check #2)
    valuation: Valuation = Valuation()
    check_valuations: dict[str, Valuation] = {}  # additional reports, keyed, never merged
    firm_comps: dict[str, ComparableAttributes] = {}  # HUMAN_ENTRY now; Pricefinder later
    security: SecurityProperty = SecurityProperty()
    screening: Screening = Screening()
    sponsor: Sponsor = Sponsor()
    sop: dict[str, SopStatement] = {}  # keyed by guarantor
    ato: AtoPosition = AtoPosition()
    insurance: dict[str, InsuranceCertificate] = {}  # keyed certificates
    rates: dict[str, RatesClearance] = {}  # land_tax, water, council
    payout: Payout = Payout()
    rfi: RfiRegister = RfiRegister()

    # Construction
    builder: Builder = Builder()
    qs: QsFindings = QsFindings()
    building_contract: BuildingContract = BuildingContract()
    da: DevelopmentApproval = DevelopmentApproval()
    equity_evidence: EquityEvidence = EquityEvidence()
    presales: dict[str, Presale] = {}
    consultants: dict[str, ConsultantReport] = {}
    tripartite: Tripartite = Tripartite()

    # Bridge
    purchase_contract: PurchaseContract = PurchaseContract()
    leases: dict[str, Lease] = {}
    da_lodgement: DaLodgement = DaLodgement()

    # Residual stock
    oc_titles: dict[str, OcTitle] = {}
    price_list: PriceList = PriceList()
    contracts_eois: dict[str, ContractEoi] = {}
    deposits_trust: DepositsTrust = DepositsTrust()

    # `marketing` slot is capture-only; no model block (DECISIONS.md 2026-06-11)

    outstanding_items: list[str] = []  # disclosed-outstanding list from empty slots + RFI
    prose: dict[int, ProseBlock] = {}  # section number â†’ prose block


def deal_type_str(model: DealModel) -> str | None:
    """The deal type as a bare string, or None if it was never extracted.

    ONE reader for a field three modules were reading three ways. `derive` and
    `ingest` tolerated either a `DealType` member or a bare string via
    `getattr(x, "value", x)`, while `materiality` assumed a member and called
    `.value` — so the same model that derived cleanly raised `AttributeError`
    when materiality ran over it.

    `apply` now normalises a bare string to the declared enum at the write path,
    which is the real fix. This stays because `apply` is not the only way a model
    gets populated — tests and `harness/populate.py` assign directly — and a
    tolerant reader costs nothing while an inconsistent one cost a run.
    """
    value = model.deal.type.value
    if value is None:
        return None
    return getattr(value, "value", value)
