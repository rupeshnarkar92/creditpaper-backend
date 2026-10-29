"""Concept registry: one definition per figure (spec 2026-09-18 §3a, DECISIONS 2026-09-18).

Documents label one figure many ways ("Facility Limit", "Loan Amount", "Total
Commitment"). Until now each extractor prompt mapped labels to fields on its own.
This module is the single reviewed mapping: a *concept* is a deal-model field path
plus what is needed to read and compare it:

- ``definition``: what the figure is, in one sentence;
- ``kind``: a ``FigureKind`` value (money, percent, number, date), or ``text``/``bool``;
- ``accepted_kinds``: the figure kinds a statement of it may be written in, ``(kind,)``
  unless the unit varies (a covenant threshold is a percent or a number; registry 2);
- ``qualifiers``: the basis a statement of it must carry to be comparable;
- ``aliases``: label forms, normalised by ``normalise_label``;
- ``sources``: the slot keys (``docs/specs/slot-taxonomy/slot_taxonomy.json``) whose
  documents may state it;
- ``authority``: the order in which those sources win for the projected value. It is
  data. It chooses a value and never hides a disagreement (§3a);
- ``compare``: the comparison rule;
- ``material``: whether the figure decides the credit (the second read, the
  contradiction register's blocking rule).

Keyed collections use the wildcard form ``[*]`` in place of a key, e.g.
``economics.funding_table[*].senior``. A concept with a fixed key
(``valuation.values_by_basis[as_is]``) is more specific than the wildcard and wins in
``concept_for``. The citation grammar (``model/citations.py``) has no wildcard of its
own: a pattern is valid when it is valid with each ``[*]`` replaced by a key.

Rules this module keeps:
- It defines no new facts. Every concept resolves to an existing ``Fact`` field.
- A label that matches no concept, or more than one, is *unmapped* (``resolve_label``
  returns None). It goes to review and is never filed under a guessed field.
- Tenants add aliases, never concepts (D1). Aliases here are the product-wide list.
- Any edit to the registry bumps ``REGISTRY_VERSION``; a test pins the registry digest
  per version, so an edit without a bump fails CI. Every assertion records the version
  it was extracted under (``Provenance.registry_version``).

Derived aggregates (``economics.total_cost``, ``metrics.*``) have no concept: no
document states them, the engine computes them (``model/derive.py``). The borrower's
funding-table block (``funding_table.*``) has none either: a funding-table document
states the *same* concepts as the ITS (the facility amount, the line totals), so it is
a source of the ``economics.*`` concepts, below the signed ITS in authority.

Alias seeding (Plan C Task 9): each group below names the extractor prompt its labels
came from (``src/creditpaper/extractors/<name>.py``), plus the common label variants of
the same figure in Australian CRE documents.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from itertools import product
from types import MappingProxyType
from typing import Any, get_args, get_origin

from pydantic import BaseModel

from creditpaper.ingest.values import FigureKind, GstBasis
from creditpaper.model.core import Fact, FactBasis, FactStatus

__all__ = [
    "KINDS",
    "REGISTRY",
    "REGISTRY_VERSION",
    "WILDCARD",
    "Compare",
    "Concept",
    "Qualifier",
    "concept_for",
    "fact_type",
    "normalise_label",
    "pattern_of",
    "qualified_fact",
    "registry_digest",
    "resolve_label",
]

REGISTRY_VERSION = "6"

WILDCARD = "*"


class Qualifier(str, Enum):
    GST_BASIS = "gst_basis"
    VALUATION_BASIS = "valuation_basis"
    AS_AT = "as_at"
    CURRENCY = "currency"
    PERIOD = "period"


class Compare(str, Enum):
    EXACT = "exact"  # to the unit (the dollar, the month)
    PCT_2DP = "pct_2dp"  # rates and percentages, to two decimal places
    # A rounded statement is consistent when the precise value rounds to it.
    STATED_PRECISION = "stated_precision"
    TEXT_NORMALISED = "text_normalised"
    DATE = "date"


# Figure kinds a concept may carry: what `values.figures` produces, less AMBIGUOUS
# (a figure that is not yet a kind), plus the two non-figure kinds.
KINDS = frozenset({k.value for k in FigureKind if k is not FigureKind.AMBIGUOUS} | {"text", "bool"})


@dataclass(frozen=True)
class Concept:
    path: str
    definition: str
    kind: str
    qualifiers: tuple[Qualifier, ...]
    aliases: tuple[str, ...]
    sources: tuple[str, ...]
    authority: tuple[str, ...]
    compare: Compare
    material: bool
    # Empty means ``(kind,)``; set in ``__post_init__`` so every concept carries it.
    accepted_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.accepted_kinds:
            object.__setattr__(self, "accepted_kinds", (self.kind,))
        if self.kind not in self.accepted_kinds or not set(self.accepted_kinds) <= KINDS:
            raise ValueError(f"{self.path}: accepted_kinds must include kind and be known kinds")


_WS = re.compile(r"\s+")


def normalise_label(label: str) -> str:
    """Casefolded, NFKC, whitespace collapsed, a trailing colon dropped."""
    text = unicodedata.normalize("NFKC", label).casefold()
    return _WS.sub(" ", text).strip().rstrip(":").strip()


_DEFAULT_COMPARE = {
    "money": Compare.EXACT,
    "percent": Compare.PCT_2DP,
    "number": Compare.EXACT,
    "date": Compare.DATE,
    "text": Compare.TEXT_NORMALISED,
    "bool": Compare.EXACT,
}

# Qualifier sets used below.
_MONEY = (Qualifier.CURRENCY,)
_MONEY_GST = (Qualifier.GST_BASIS, Qualifier.CURRENCY)
_VALUE = (Qualifier.VALUATION_BASIS, Qualifier.AS_AT, Qualifier.GST_BASIS, Qualifier.CURRENCY)
_RATE = (Qualifier.PERIOD,)

# Slot keys (docs/specs/slot-taxonomy/slot_taxonomy.json).
_ITS = "signed_its"
_LOANDOC = "loan_documentation"
_FT = "funding_table"
_VAL = "valuation"
_QS = "qs_report"
_BC = "building_contract"
_DA = "development_approval"
_INSURANCE = ("public_liability_insurance", "contract_works_insurance", "building_insurance")
_BUILDER = ("builder_financials", "builder_licence", "builder_profile")
_KYC = "kyc_screening"
_STRUCT = "corporate_structure"
_PROFILE = "borrower_profile"
_FIN = "borrower_financials"


def _c(
    path: str,
    definition: str,
    kind: str,
    sources: str | tuple[str, ...],
    aliases: tuple[str, ...] = (),
    *,
    qualifiers: tuple[Qualifier, ...] = (),
    authority: tuple[str, ...] | None = None,
    compare: Compare | None = None,
    material: bool = False,
    accepted_kinds: tuple[str, ...] = (),
) -> Concept:
    srcs = (sources,) if isinstance(sources, str) else sources
    return Concept(
        path=path,
        definition=definition,
        kind=kind,
        qualifiers=qualifiers,
        aliases=tuple(normalise_label(a) for a in aliases),
        sources=srcs,
        authority=srcs if authority is None else authority,
        compare=compare or _DEFAULT_COMPARE[kind],
        material=material,
        accepted_kinds=accepted_kinds,
    )


_CONCEPTS: tuple[Concept, ...] = (
    # --- extractors/its.py: facility economics (D5: the signed ITS wins) ---------------
    _c(
        "deal.type",
        "The facility type: construction, bridge or residual stock.",
        "text",
        _ITS,
        ("facility type", "loan type", "type of facility", "facility purpose"),
        material=True,
    ),
    _c(
        "economics.facility_amount",
        "The total facility limit the lender commits.",
        "money",
        (_ITS, _LOANDOC, _FT),
        (
            "total facility limit",
            "facility limit",
            "facility amount",
            "total facility",
            "loan amount",
            "loan limit",
            "total loan amount",
            "total commitment",
        ),
        qualifiers=_MONEY,
        material=True,
    ),
    _c(
        "economics.interest_rate",
        "The interest rate on the facility, % per annum.",
        "percent",
        (_ITS, _LOANDOC),
        ("interest rate", "rate of interest", "coupon", "coupon rate", "interest rate (p.a.)"),
        qualifiers=_RATE,
        material=True,
    ),
    _c(
        "economics.term_months",
        "The facility term, in months.",
        "number",
        (_ITS, _LOANDOC),
        ("term", "loan term", "facility term", "term (months)", "tenor", "loan period"),
        qualifiers=_RATE,
        material=True,
    ),
    _c(
        "economics.fees[establishment].pct",
        "The establishment fee as a % of the facility.",
        "percent",
        (_ITS, _LOANDOC),
        (
            "establishment fee",
            "establishment fee (%)",
            "establishment fee %",
            "upfront fee",
            "arrangement fee",
        ),
        material=True,
    ),
    _c(
        "economics.fees[establishment].amount",
        "The establishment fee in dollars.",
        "money",
        (_ITS, _LOANDOC, _FT),
        ("establishment fee amount", "establishment fee ($)", "establishment fees"),
        qualifiers=_MONEY_GST,
        material=True,
    ),
    _c(
        "economics.fees[line].pct",
        "The line fee as a % of the facility, per annum.",
        "percent",
        (_ITS, _LOANDOC),
        ("line fee", "line fee (%)", "line fee %", "line fee (p.a.)"),
        qualifiers=_RATE,
        material=True,
    ),
    _c(
        "economics.fees[line].amount",
        "The line fee in dollars.",
        "money",
        (_ITS, _LOANDOC, _FT),
        ("line fee amount", "line fee ($)", "line fees"),
        qualifiers=_MONEY_GST,
        material=True,
    ),
    _c(
        "economics.funding_table[*].total",
        "A funding-table line's total cost.",
        "money",
        (_ITS, _FT),
        ("total cost", "total", "total costs"),
        qualifiers=_MONEY_GST,
        material=True,
    ),
    _c(
        "economics.funding_table[*].senior",
        "The senior debt funding a funding-table line.",
        "money",
        (_ITS, _FT),
        ("senior", "senior debt", "senior facility", "senior loan"),
        qualifiers=_MONEY_GST,
        material=True,
    ),
    _c(
        "its.borrower_entity",
        "The borrowing entity, as named.",
        "text",
        (_ITS, _LOANDOC),
        ("borrower", "borrower entity", "borrowing entity", "the borrower"),
        material=True,
    ),
    _c(
        "its.guarantors[*]",
        "A guarantor of the facility, as named.",
        "text",
        (_ITS, _LOANDOC),
        ("guarantor", "guarantors", "guarantor(s)"),
        material=True,
    ),
    _c(
        "its.signed_date",
        "The date the ITS was signed.",
        "date",
        _ITS,
        ("date signed", "signed date", "execution date", "date of execution"),
    ),
    _c(
        "covenants[*].threshold_value",
        "A financial covenant's threshold, as a number (45 for 45%, 1.5 for 1.5x).",
        "number",
        (_ITS, _LOANDOC),
        (
            "covenant",
            "covenant threshold",
            "financial covenant",
            "maximum lvr",
            "max lvr",
            "maximum loan to value ratio",
            "lvr covenant",
        ),
        compare=Compare.PCT_2DP,
        material=True,
        # Written as a percent (LVR, LCR) or a number or multiple (ICR "1.5x").
        accepted_kinds=("percent", "number"),
    ),
    _c(
        "covenants[*].summary",
        "A financial covenant, restated as the ITS words it.",
        "text",
        (_ITS, _LOANDOC),
    ),
    # --- extractors/valuation.py: the security (headline pass) ------------------------
    _c(
        "valuation.valuer",
        "The valuation firm and signing valuer.",
        "text",
        _VAL,
        ("valuer", "valuation firm", "valued by", "prepared by", "instructing valuer"),
        material=True,
    ),
    _c(
        "valuation.valuation_date",
        "The date AS AT which the property is valued (often the inspection date), not the date the report "
        "was issued (that is valuation.report_date).",
        "date",
        _VAL,
        (
            "date of valuation",
            "valuation date",
            "effective date of valuation",
            "date of value",
            "date of inspection and valuation",
            "inspection and valuation date",
        ),
        material=True,
    ),
    _c(
        "valuation.report_date",
        "The date the valuation report was issued or signed; not the date of valuation.",
        "date",
        _VAL,
        (
            "date of valuation report",
            "date of the valuation report",
            "report date",
            "date of report",
            "date issued",
        ),
    ),
    _c(
        "valuation.values_by_basis[as_is]",
        "The market value of the property in its current state.",
        "money",
        _VAL,
        (
            "as is",
            "as is value",
            "as is market value",
            "market value as is",
            "market value (as is)",
            "current market value",
            "value as is",
            "as is land value",
        ),
        qualifiers=_VALUE,
        material=True,
    ),
    _c(
        "valuation.values_by_basis[as_if_complete]",
        "The market value on completion, excluding GST.",
        "money",
        _VAL,
        (
            "as if complete",
            "as if complete value",
            "as if complete (ex gst)",
            "as if complete (excl gst)",
            "as if complete market value",
            "market value as if complete",
            "value on completion",
            "on completion value",
        ),
        qualifiers=_VALUE,
        material=True,
    ),
    _c(
        "valuation.values_by_basis[as_if_complete_incl_gst]",
        "The market value on completion, including GST.",
        "money",
        _VAL,
        ("as if complete (incl gst)", "as if complete (inc gst)", "as if complete value (incl gst)"),
        qualifiers=_VALUE,
        material=True,
    ),
    _c(
        "valuation.values_by_basis[gross_realisation]",
        "The gross realisation: the sum of the individual sale values on completion.",
        "money",
        _VAL,
        (
            "gross realisation",
            "gross realisation value",
            "gross realisations",
            "grv",
            "total gross realisation",
        ),
        qualifiers=_VALUE,
        material=True,
    ),
    _c(
        "valuation.values_by_basis[as_if_complete_one_line]",
        "The market value on completion, sold in one line (en bloc).",
        "money",
        _VAL,
        (
            "in one line",
            "as if complete in one line",
            "as if complete (one line)",
            "one line value",
            "en bloc",
        ),
        qualifiers=_VALUE,
        material=True,
    ),
    _c(
        "valuation.methodology.approach",
        "The valuation approach (direct comparison, capitalisation, hypothetical development).",
        "text",
        _VAL,
        ("valuation approach", "methodology", "valuation methodology", "method of valuation", "approach"),
    ),
    _c(
        "valuation.property.address",
        "The subject property's address.",
        "text",
        _VAL,
        ("property address", "address", "subject property", "site address"),
        material=True,
    ),
    _c(
        "valuation.property.title_reference",
        "The subject property's title reference.",
        "text",
        _VAL,
        ("title reference", "title", "real property description", "legal description", "title particulars"),
        material=True,
    ),
    _c(
        "valuation.property.zoning",
        "The subject property's zoning.",
        "text",
        _VAL,
        ("zoning", "zone", "land zoning"),
    ),
    _c(
        "valuation.property.site_area",
        "The subject site's area, in square metres.",
        "number",
        _VAL,
        ("site area", "land area", "site size", "area of land"),
        compare=Compare.STATED_PRECISION,
    ),
    _c(
        "valuation.property.gfa_total",
        "The subject's total gross floor area, in square metres.",
        "number",
        _VAL,
        ("gfa", "gross floor area", "total gfa", "total gross floor area"),
        compare=Compare.STATED_PRECISION,
    ),
    # --- extractors/valuation.py: market and site pass --------------------------------
    _c(
        "valuation.property.planning_controls",
        "The planning controls on the site (height, FSR, overlays).",
        "text",
        _VAL,
        ("planning controls", "planning control"),
    ),
    _c(
        "valuation.subject_attributes.location",
        "The subject site's physical characteristics and location.",
        "text",
        _VAL,
        ("site characteristics", "site description", "location"),
    ),
    _c(
        "valuation.market.conditions",
        "The valuer's view of market conditions.",
        "text",
        _VAL,
        ("market conditions", "market commentary"),
    ),
    _c(
        "valuation.market.supply_demand",
        "The valuer's view of supply and demand for the product.",
        "text",
        _VAL,
        ("supply and demand", "supply/demand", "supply & demand"),
    ),
    _c(
        "valuation.market.locality_outlook",
        "The valuer's outlook for the locality.",
        "text",
        _VAL,
        ("locality outlook", "outlook"),
    ),
    _c(
        "valuation.market.market_risks[*]",
        "A market risk the valuer names.",
        "text",
        _VAL,
        ("market risks", "market risk"),
    ),
    _c(
        "valuation.property_risks[*]",
        "A property risk the valuer names.",
        "text",
        _VAL,
        ("property risks", "property risk"),
    ),
    # --- extractors/valuation.py: comparable sales pass -------------------------------
    # Comparable cells share labels with the subject ("land area"): the subject owns the
    # alias, and comparable cells are mapped by their table, not by label alone.
    _c("valuation.comp_set[*].location", "A comparable sale's address.", "text", _VAL),
    _c(
        "valuation.comp_set[*].sale_price",
        "A comparable's sale price.",
        "money",
        _VAL,
        ("sale price",),
        qualifiers=_MONEY_GST,
    ),
    _c(
        "valuation.comp_set[*].sale_date",
        "A comparable's sale date.",
        "date",
        _VAL,
        ("sale date", "date of sale"),
    ),
    _c(
        "valuation.comp_set[*].derived_rate",
        "A comparable's derived rate, $ per square metre of the relevant measure.",
        "money",
        _VAL,
        ("rate per sqm", "$/sqm", "$/m2", "rate psm", "derived rate", "analysed rate"),
        qualifiers=_MONEY_GST,
    ),
    _c(
        "valuation.comp_set[*].land_area",
        "A comparable's land area, in square metres.",
        "number",
        _VAL,
        compare=Compare.STATED_PRECISION,
    ),
    _c(
        "valuation.comp_set[*].gfa_nla",
        "A comparable's GFA or NLA, in square metres.",
        "number",
        _VAL,
        ("gfa/nla", "nla", "net lettable area"),
        compare=Compare.STATED_PRECISION,
    ),
    _c(
        "valuation.comp_set[*].adjustment",
        "The valuer's adjustment of a comparable to the subject.",
        "text",
        _VAL,
        ("adjustment",),
    ),
    _c("valuation.comp_set[*].adjustment_reason", "Why the valuer adjusted a comparable.", "text", _VAL),
    _c("valuation.comp_set[*].condition", "A comparable's condition.", "text", _VAL, ("condition",)),
    # --- extractors/qs.py -------------------------------------------------------------
    _c(
        "qs.firm",
        "The financier's quantity surveyor.",
        "text",
        _QS,
        ("quantity surveyor", "qs", "qs firm", "prepared by"),
    ),
    _c(
        "qs.project_cost",
        "The total project (development) cost the QS assesses.",
        "money",
        _QS,
        (
            "total project cost",
            "project cost",
            "total development cost",
            "development cost",
            "total construction cost",
            "tdc",
        ),
        qualifiers=_MONEY_GST,
        material=True,
    ),
    _c(
        "qs.cost_to_complete",
        "The remaining cost to complete the project.",
        "money",
        _QS,
        ("cost to complete", "costs to complete", "ctc", "remaining cost", "balance to complete"),
        qualifiers=_MONEY_GST,
        material=True,
    ),
    _c(
        "qs.contract_sum_view",
        "The QS's view of the builder's contract sum.",
        "text",
        _QS,
        ("contract sum assessment", "comment on contract sum"),
    ),
    _c(
        "qs.agreement_appropriate",
        "Whether the QS finds the construction contract appropriate.",
        "bool",
        _QS,
        ("contract appropriate", "construction contract appropriate"),
        material=True,
    ),
    _c(
        "qs.cost_lines[*].amount",
        "A QS cost line's amount, on the line's stated basis.",
        "money",
        _QS,
        qualifiers=_MONEY_GST,
    ),
    _c("qs.cost_lines[*].label", "A QS cost line, in the QS's own words.", "text", _QS),
    _c(
        "qs.cost_lines[*].basis",
        "The column a QS cost line was read from: total_cost, cost_to_complete or costs_to_date.",
        "text",
        _QS,
    ),
    _c(
        "qs.overrun_risks[*]",
        "A cost or programme overrun risk the QS flags.",
        "text",
        _QS,
        ("overrun risks", "risks"),
    ),
    _c(
        "qs.items_not_in_feasibility[*]",
        "A cost the QS says the developer's feasibility omits.",
        "text",
        _QS,
        ("items not in feasibility", "exclusions"),
    ),
    # --- extractors/building_contract.py (QS restates the contract sum) ---------------
    _c(
        "building_contract.contract_sum",
        "The building contract sum.",
        "money",
        (_BC, _QS),
        (
            "contract sum",
            "contract price",
            "lump sum price",
            "contract value",
            "fixed price",
            "building contract sum",
        ),
        qualifiers=_MONEY_GST,
        material=True,
    ),
    _c(
        "building_contract.contract_type",
        "The contract type (lump sum, cost-plus, design and construct).",
        "text",
        _BC,
        ("contract type", "type of contract", "form of contract"),
        material=True,
    ),
    _c(
        "building_contract.retention.amount",
        "The retention held under the building contract, in dollars.",
        "money",
        _BC,
        ("retention", "retention amount", "retention sum"),
        qualifiers=_MONEY_GST,
    ),
    _c(
        "building_contract.retention.timing",
        "The retention rate and when it is released.",
        "text",
        _BC,
        ("retention release", "release of retention"),
    ),
    _c(
        "building_contract.defects_liability_period",
        "The defects liability period.",
        "text",
        _BC,
        ("defects liability period", "dlp", "defects period"),
    ),
    _c(
        "building_contract.programme",
        "The construction programme or practical-completion date.",
        "text",
        _BC,
        ("programme", "program", "construction period", "construction programme", "time for completion"),
        material=True,
    ),
    _c("building_contract.key_clauses[*]", "A material building-contract clause, restated.", "text", _BC),
    _c(
        "building_contract.tripartite_present",
        "Whether a tripartite (step-in) deed exists.",
        "bool",
        (_BC, "tripartite"),
        ("tripartite deed", "tripartite agreement", "side deed", "step-in deed", "collateral warranty"),
        material=True,
    ),
    _c(
        "building_contract.step_in_rights",
        "The lender's step-in rights on builder default, restated.",
        "text",
        _BC,
        ("step-in rights", "step in rights", "lender step-in"),
    ),
    # --- extractors/da.py: the planning approval --------------------------------------
    _c(
        "da.instrument_type",
        "The kind of planning approval, as named.",
        "text",
        _DA,
        ("type of approval", "approval type", "consent type"),
    ),
    _c(
        "da.reference",
        "The approval's identifier (DA, permit or application number).",
        "text",
        _DA,
        ("da number", "da no", "application number", "application no", "permit number", "consent number"),
        material=True,
    ),
    _c(
        "da.authority",
        "The consent or responsible authority.",
        "text",
        _DA,
        ("consent authority", "responsible authority", "assessment manager", "council", "determined by"),
    ),
    _c(
        "da.approved_date",
        "The date the approval was granted.",
        "date",
        _DA,
        (
            "date of determination",
            "determination date",
            "approval date",
            "date of approval",
            "date of decision",
            "decision date",
        ),
        material=True,
    ),
    _c(
        "da.approved_description",
        "What the approval allows to be built.",
        "text",
        _DA,
        (
            "description of development",
            "proposed development",
            "development description",
            "approved development",
        ),
    ),
    _c("da.approved_use", "The approved use.", "text", _DA, ("approved use", "land use")),
    _c(
        "da.build_ready",
        "Whether the approval is operative and authorises commencement.",
        "bool",
        _DA,
        material=True,
    ),
    _c("da.build_ready_basis", "Why the approval is or is not build-ready, in its own words.", "text", _DA),
    _c(
        "da.lapse_date",
        "The date the approval lapses, where one is stated.",
        "date",
        _DA,
        ("lapse date", "expiry date", "date of lapse", "permit expiry"),
        material=True,
    ),
    _c(
        "da.lapse_basis",
        "The approval's currency rule, as stated.",
        "text",
        _DA,
        ("lapsing", "currency period", "expiry of permit", "lapsing of consent"),
    ),
    _c("da.approval_current", "Whether the approval is in force.", "bool", _DA, material=True),
    _c("da.conditions[*].text", "A credit-material approval condition, restated.", "text", _DA),
    _c("da.conditions[*].status", "A condition's status (satisfied, outstanding, deferred).", "text", _DA),
    _c("da.conditions[*].blocker", "Whether a condition gates commencement or occupation.", "bool", _DA),
    _c(
        "da.contributions[*].description",
        "A monetary obligation under the approval (contribution, levy, bond).",
        "text",
        (_DA, "authority_contributions"),
    ),
    _c(
        "da.contributions[*].amount",
        "A contribution's amount, where stated on the approval.",
        "money",
        (_DA, "authority_contributions"),
        ("contribution", "contribution amount", "contributions"),
        qualifiers=_MONEY,
        authority=("authority_contributions", _DA),
    ),
    _c(
        "da.contributions[*].trigger",
        "When a contribution is payable.",
        "text",
        (_DA, "authority_contributions"),
    ),
    _c(
        "da.contributions[*].amount_on_separate_notice",
        "Whether a contribution's amount is set on a separate notice.",
        "bool",
        (_DA, "authority_contributions"),
    ),
    _c(
        "da.build_authorisation.kind",
        "The downstream approval to build, as the jurisdiction names it.",
        "text",
        (_DA, "construction_certificate"),
    ),
    _c(
        "da.build_authorisation.reference",
        "The build authorisation's reference.",
        "text",
        (_DA, "construction_certificate"),
    ),
    _c(
        "da.build_authorisation.status",
        "Whether the build authorisation is issued.",
        "text",
        (_DA, "construction_certificate"),
        authority=("construction_certificate", _DA),
    ),
    _c(
        "da.occupation_authorisation.kind",
        "The downstream approval to occupy, as the jurisdiction names it.",
        "text",
        (_DA, "occupancy_certificate"),
    ),
    _c(
        "da.occupation_authorisation.reference",
        "The occupation authorisation's reference.",
        "text",
        (_DA, "occupancy_certificate"),
    ),
    _c(
        "da.occupation_authorisation.status",
        "Whether the occupation authorisation is issued.",
        "text",
        (_DA, "occupancy_certificate"),
        authority=("occupancy_certificate", _DA),
    ),
    # --- extractors/insurance.py ------------------------------------------------------
    _c(
        "insurance[*].cover_type",
        "The class of insurance cover.",
        "text",
        _INSURANCE,
        ("cover type", "type of cover", "class of insurance", "policy type"),
    ),
    _c(
        "insurance[*].sum",
        "The sum insured or limit of indemnity.",
        "money",
        _INSURANCE,
        ("sum insured", "limit of indemnity", "insured amount", "total sum insured"),
        qualifiers=_MONEY,
        material=True,
    ),
    _c(
        "insurance[*].period_start",
        "The start of the period of insurance.",
        "date",
        _INSURANCE,
        ("period of insurance from", "inception date", "policy start"),
    ),
    _c(
        "insurance[*].period_end",
        "The end of the period of insurance.",
        "date",
        _INSURANCE,
        ("period of insurance to", "expiry date", "policy expiry"),
        material=True,
    ),
    _c(
        "insurance[*].parties_noted[*]",
        "A named insured or interested party.",
        "text",
        _INSURANCE,
        ("insured", "named insured", "interested parties", "interested party", "insured parties"),
    ),
    # --- extractors/builder.py --------------------------------------------------------
    _c(
        "builder.licence.number",
        "The builder's licence number.",
        "text",
        _BUILDER,
        ("licence number", "licence no", "contractor licence number", "builder's licence"),
        authority=("builder_licence", "builder_profile", "builder_financials"),
    ),
    _c(
        "builder.licence.category",
        "The class of work the builder's licence covers.",
        "text",
        _BUILDER,
        ("licence class", "licence category", "class of work"),
        authority=("builder_licence", "builder_profile", "builder_financials"),
    ),
    _c(
        "builder.licence.current",
        "Whether the builder's licence is current.",
        "bool",
        _BUILDER,
        ("licence status",),
        authority=("builder_licence", "builder_profile", "builder_financials"),
        material=True,
    ),
    _c(
        "builder.financials.cash_position",
        "The builder's cash position or net assets, as stated.",
        "money",
        _BUILDER,
        ("cash position", "cash", "cash at bank", "net assets"),
        qualifiers=_MONEY,
        authority=("builder_financials", "builder_profile", "builder_licence"),
        material=True,
    ),
    _c(
        "builder.site_manager",
        "The engaged site manager or nominated supervisor.",
        "text",
        _BUILDER,
        ("site manager", "nominated supervisor", "supervisor"),
    ),
    _c(
        "builder.financials.defaults[*]",
        "A default, judgment or insolvency event on the builder's record.",
        "text",
        _BUILDER,
    ),
    # --- extractors/screening.py (conclusions only, D7) -------------------------------
    _c(
        "screening.aml.provider",
        "The AML and sanctions screening provider.",
        "text",
        _KYC,
        ("screening provider", "provider"),
    ),
    _c(
        "screening.aml.sanctions",
        "Whether screening found a sanctions hit.",
        "bool",
        _KYC,
        ("sanctions", "sanctions hit"),
        material=True,
    ),
    _c(
        "screening.aml.pep",
        "Whether a party is a politically exposed person.",
        "bool",
        _KYC,
        ("pep", "politically exposed person"),
        material=True,
    ),
    _c(
        "screening.aml.adverse_media",
        "Whether screening found adverse media.",
        "bool",
        _KYC,
        ("adverse media",),
        material=True,
    ),
    _c(
        "screening.identity.verified",
        "Whether identity was verified.",
        "bool",
        _KYC,
        ("identity verified", "id verification", "identity verification"),
        material=True,
    ),
    _c("screening.credit.adverse_findings[*]", "An adverse credit finding, as a conclusion.", "text", _KYC),
    # --- extractors/sponsor.py --------------------------------------------------------
    _c(
        "sponsor.cash_equity_to_date",
        "The cumulative cash equity the sponsor has contributed.",
        "money",
        (_FIN, _PROFILE),
        ("cash equity", "cash equity contributed", "equity contributed", "equity to date"),
        qualifiers=_MONEY,
        material=True,
    ),
    _c(
        "sponsor.liquidity_cushion",
        "Unused cash in the borrowing entity.",
        "money",
        (_FIN, _PROFILE),
        ("liquidity", "cash reserves", "unencumbered cash", "liquid assets"),
        qualifiers=_MONEY,
        material=True,
    ),
    _c(
        "sponsor.financials[*].revenue_trend",
        "A financial year's revenue trend, as a phrase.",
        "text",
        _FIN,
        ("revenue trend",),
    ),
    _c(
        "sponsor.financials[*].profitability",
        "A financial year's net result (a loss is negative).",
        "money",
        _FIN,
        ("net profit", "net profit after tax", "npat", "net result"),
        qualifiers=(Qualifier.PERIOD, Qualifier.CURRENCY),
        material=True,
    ),
    _c(
        "sponsor.financials[*].debt_levels",
        "A financial year's total or interest-bearing debt.",
        "money",
        _FIN,
        ("total debt", "borrowings", "interest-bearing debt", "interest bearing liabilities"),
        qualifiers=(Qualifier.AS_AT, Qualifier.CURRENCY),
        material=True,
    ),
    _c(
        "sponsor.financials[*].going_concern",
        "Whether going concern is confirmed.",
        "bool",
        _FIN,
        ("going concern",),
        material=True,
    ),
    _c(
        "sponsor.financials[*].contingent_liabilities[*]",
        "A contingent liability the notes disclose.",
        "text",
        _FIN,
        ("contingent liabilities",),
    ),
    _c(
        "sponsor.financials[*].related_party_transactions[*]",
        "A related-party transaction the notes disclose.",
        "text",
        _FIN,
        ("related party transactions", "related-party transactions"),
    ),
    _c("sponsor.structure.entities[*].name", "A group entity's name, as written.", "text", _STRUCT),
    _c("sponsor.structure.entities[*].entity_type", "A group entity's type.", "text", _STRUCT),
    _c("sponsor.structure.entities[*].role", "A group entity's role in the deal.", "text", _STRUCT),
    _c("sponsor.structure.trusts[*].name", "A trust's name, as written.", "text", _STRUCT),
    _c("sponsor.structure.trusts[*].entity_type", "A trust's type.", "text", _STRUCT),
    _c("sponsor.structure.trusts[*].role", "A trust's role in the deal.", "text", _STRUCT),
    _c(
        "sponsor.structure.ubos[*]",
        "An ultimate beneficial owner, as identified.",
        "text",
        _STRUCT,
        ("ultimate beneficial owner", "ubo", "beneficial owner", "ultimate beneficial owners"),
    ),
    _c("sponsor.claims[*].claim", "A testable track-record claim.", "text", _PROFILE),
    _c("sponsor.claims[*].project", "The project a track-record claim concerns.", "text", _PROFILE),
    _c("sponsor.claims[*].asset_type", "A track-record project's asset type.", "text", _PROFILE),
    _c("sponsor.claims[*].scale", "A track-record project's scale, as stated.", "text", _PROFILE),
    _c("sponsor.claims[*].year", "A track-record project's completion year.", "number", _PROFILE),
    _c("sponsor.claims[*].verifiable_detail", "What makes a track-record claim checkable.", "text", _PROFILE),
    # --- 2026-09-24: the extracted NUMBER and DATE paths that had no concept --------
    #
    # A path with no concept gets no semantic support check, and until today it
    # got one silently: the fact reached VERIFIED at 0.95 looking exactly like
    # one that had been checked. `Reason.SUPPORT_UNAVAILABLE` now records the
    # absence, and these concepts remove it for the fields a credit paper's
    # numbers actually come from.
    #
    # NOT added, deliberately: `valuation.values_by_basis[*]` and
    # `economics.fees[*]` keep their FIXED keys. An unknown valuation basis or
    # fee type must stay unmapped so it goes to review rather than being filed
    # under a guessed field - the rule at the top of this module. A wildcard
    # there would convert "we do not know what this is" into a confident write.
    #
    # `metrics.*`, `economics.total_cost`, `economics.senior_total` and
    # `economics.equity_total` are DERIVED by `model/derive.py`, never
    # extracted, so they correctly have no extraction concept either.
    # The third funding-table column. `total` and `senior` were mapped and
    # `equity` was not, so the one line of the three that says how much of the
    # cost the sponsor carries had no concept.
    _c(
        "economics.funding_table[*].equity",
        "The sponsor equity funding a funding-table line.",
        "money",
        (_ITS, _FT),
        ("equity", "sponsor equity", "borrower equity", "cash equity"),
        qualifiers=_MONEY_GST,
        material=True,
    ),
    # --- funding_table: deliberately NO concepts ------------------------------------
    # DECISIONS.md 2026-09-19 (registry v1) ruled it: "the borrower's
    # `funding_table.*` block have no concept: the funding-table document is a
    # lower-authority source of the `economics.*` concepts instead". A figure
    # read from that document is filed under `economics.*`, where the ITS
    # outranks it. Concepts here were added on 2026-09-24 and removed the same
    # day on finding the entry - they would have split one figure across two
    # concepts and taken the authority ordering with it.
    # --- purchase contract ---------------------------------------------------------
    _c(
        "purchase_contract.price",
        "The purchase price the contract of sale states for the property.",
        "money",
        "purchase_contract",
        ("purchase price", "price", "contract price", "consideration", "sale price"),
        qualifiers=_MONEY_GST,
        material=True,
    ),
    _c(
        "purchase_contract.settlement_date",
        "The date settlement of the purchase is due under the contract.",
        "date",
        "purchase_contract",
        ("settlement date", "date for completion", "completion date", "settlement"),
        material=True,
    ),
    # --- presales ------------------------------------------------------------------
    _c(
        "presales[*].price",
        "The sale price of one presold lot under its contract.",
        "money",
        "presales",
        ("price", "sale price", "purchase price", "contract price"),
        qualifiers=_MONEY_GST,
        material=True,
    ),
    _c(
        "presales[*].deposit.amount",
        "The deposit held for one presold lot.",
        "money",
        ("presales", "deposits_in_trust"),
        ("deposit", "deposit amount", "deposit held", "deposit paid"),
        qualifiers=_MONEY,
    ),
    # --- leases --------------------------------------------------------------------
    _c(
        "leases[*].rent",
        "The rent payable under one lease, as stated.",
        "money",
        "leases",
        ("rent", "annual rent", "rental", "passing rent", "gross rent", "net rent"),
        qualifiers=(Qualifier.CURRENCY, Qualifier.PERIOD, Qualifier.GST_BASIS),
    ),
    _c(
        "leases[*].expiry",
        "The date one lease expires.",
        "date",
        "leases",
        ("expiry", "expiry date", "lease expiry", "termination date", "end date"),
    ),
    # --- equity evidence -----------------------------------------------------------
    _c(
        "equity_evidence.total",
        "The total sponsor equity evidenced as already contributed.",
        "money",
        "equity_evidence",
        ("total equity", "equity contributed", "equity to date", "total contribution"),
        qualifiers=_MONEY_GST,
        material=True,
    ),
    _c(
        "equity_evidence.invoices[*].amount",
        "The amount of one invoice evidencing equity already spent.",
        "money",
        "equity_evidence",
        ("amount", "invoice amount", "invoice total", "amount paid"),
        qualifiers=_MONEY_GST,
    ),
    _c(
        "equity_evidence.invoices[*].date",
        "The date of one invoice evidencing equity already spent.",
        "date",
        "equity_evidence",
        ("date", "invoice date", "date of invoice", "tax invoice date"),
    ),
    # --- deposits, payout, rates, lodgement, funding date --------------------------
    _c(
        "deposits_trust.total",
        "The total of purchaser deposits held in trust.",
        "money",
        "deposits_in_trust",
        ("total deposits", "deposits held", "held in trust", "trust balance"),
        qualifiers=_MONEY,
        material=True,
    ),
    _c(
        "payout.amount",
        "The amount required to discharge the existing debt.",
        "money",
        "payout_figure",
        ("payout", "payout figure", "payout amount", "discharge amount", "amount to discharge"),
        qualifiers=_MONEY,
        material=True,
    ),
    _c(
        "rates[*].issue_date",
        "The issue date of one rates or land tax notice.",
        "date",
        ("council_rates", "water_rates", "land_tax_clearance"),
        ("issue date", "date of issue", "issued", "notice date"),
    ),
    _c(
        "da_lodgement.lodgement_date",
        "The date the development application was lodged with the authority.",
        "date",
        "da_submission_pack",
        ("lodgement date", "date lodged", "lodged", "date of lodgement"),
    ),
    _c(
        "deal.expected_funding_date",
        "The date the facility is expected to fund.",
        "date",
        (_ITS, "rfi_sheet"),
        ("expected funding date", "funding date", "drawdown date", "settlement date"),
    ),
    _c(
        "contracts_eois[*].price",
        "The price stated in one contract or expression of interest.",
        "money",
        "contracts_eois",
        ("price", "sale price", "offer price", "purchase price"),
        qualifiers=_MONEY_GST,
    ),
    _c(
        "price_list.lots[*]",
        "The listed price of one lot on the price list.",
        "money",
        "price_list",
        ("price", "list price", "asking price", "lot price"),
        qualifiers=_MONEY_GST,
    ),
    # --- statement of position -----------------------------------------------------
    _c(
        "sop[*].free_cash",
        "Free cash a party's statement of position states.",
        "money",
        "statement_of_position",
        ("free cash", "cash", "cash at bank", "liquid funds", "cash available"),
        qualifiers=_MONEY,
        material=True,
    ),
    _c(
        "sop[*].net_position",
        "The net asset position a party's statement of position states.",
        "money",
        "statement_of_position",
        ("net position", "net assets", "net worth", "net asset position"),
        qualifiers=_MONEY,
        material=True,
    ),
    _c(
        "sop[*].assets[*].value",
        "The stated value of one asset in a statement of position.",
        "money",
        "statement_of_position",
        ("value", "asset value", "estimated value", "market value"),
        qualifiers=_MONEY,
    ),
    _c(
        "sop[*].liabilities[*].amount",
        "The stated amount of one liability in a statement of position.",
        "money",
        "statement_of_position",
        ("amount", "liability", "balance", "amount owing", "debt"),
        qualifiers=_MONEY,
    ),
    # --- QS divergence -------------------------------------------------------------
    _c(
        "qs.divergence_vs_developer[*].qs_figure",
        "The quantity surveyor's figure for a cost line.",
        "money",
        _QS,
        ("qs figure", "qs assessment", "qs cost", "assessed"),
        qualifiers=_MONEY_GST,
    ),
    _c(
        "qs.divergence_vs_developer[*].developer_figure",
        "The developer's own figure for the same cost line.",
        "money",
        _QS,
        ("developer figure", "developer cost", "builder figure", "as submitted"),
        qualifiers=_MONEY_GST,
    ),
    # --- the subject property's own attributes, twins of comp_set[*] ----------------
    _c(
        "valuation.subject_attributes.sale_price",
        "The subject property's own sale price, where the valuation states one.",
        "money",
        _VAL,
        (),  # the twin in this slot owns these labels
        qualifiers=_MONEY_GST,
    ),
    _c(
        "valuation.subject_attributes.sale_date",
        "The date the subject property last sold, where the valuation states one.",
        "date",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "valuation.subject_attributes.gfa_nla",
        "The subject property's gross floor area or net lettable area.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "valuation.subject_attributes.land_area",
        "The subject property's land area.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "valuation.subject_attributes.derived_rate",
        "The subject property's rate per unit of area.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    # --- firm_comps[*]: the firm's own comparable evidence, twins of comp_set[*] ----
    _c(
        "firm_comps[*].sale_price",
        "A firm comparable's sale price.",
        "money",
        _VAL,
        (),  # the twin in this slot owns these labels
        qualifiers=_MONEY_GST,
    ),
    _c(
        "firm_comps[*].sale_date",
        "The date a firm comparable sold.",
        "date",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "firm_comps[*].gfa_nla",
        "A firm comparable's gross floor area or net lettable area.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "firm_comps[*].land_area",
        "A firm comparable's land area.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "firm_comps[*].derived_rate",
        "A firm comparable's rate per unit of area.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    # --- check_valuations[*]: a second valuation, read for cross-check ---------------
    # --- check_valuations[*]: a second valuation, read for cross-check ---------------
    # A check valuation reports the SAME bases as the primary one, so the five
    # fixed keys are mirrored rather than replaced by a wildcard: an unknown
    # basis must stay unmapped and go to review here too.
    _c(
        "check_valuations[*].values_by_basis[as_is]",
        "A check valuation's market value of the property in its current state.",
        "money",
        _VAL,
        (),  # the twin in this slot owns these labels
        qualifiers=_VALUE,
        material=True,
    ),
    _c(
        "check_valuations[*].values_by_basis[as_if_complete]",
        "A check valuation's market value on completion, excluding GST.",
        "money",
        _VAL,
        (),  # valuation.values_by_basis owns these labels in this slot
        qualifiers=_VALUE,
        material=True,
    ),
    _c(
        "check_valuations[*].values_by_basis[as_if_complete_incl_gst]",
        "A check valuation's market value on completion, including GST.",
        "money",
        _VAL,
        (),  # valuation.values_by_basis owns these labels in this slot
        qualifiers=_VALUE,
        material=True,
    ),
    _c(
        "check_valuations[*].values_by_basis[gross_realisation]",
        "A check valuation's gross realisation on completion.",
        "money",
        _VAL,
        (),  # the twin in this slot owns these labels
        qualifiers=_VALUE,
        material=True,
    ),
    _c(
        "check_valuations[*].values_by_basis[as_if_complete_one_line]",
        "A check valuation's market value on completion, sold in one line.",
        "money",
        _VAL,
        (),  # the twin in this slot owns these labels
        qualifiers=_VALUE,
        material=True,
    ),
    # Same meanings as the primary valuation's fields; the block differs only in
    # which document the figure came from, which provenance already records.
    _c(
        "check_valuations[*].valuation_date",
        "The date of valuation of a check valuation.",
        "date",
        _VAL,
        (),  # the twin in this slot owns these labels
        material=True,
    ),
    _c(
        "check_valuations[*].property.gfa_total",
        "A check valuation's total gross floor area for the property.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "check_valuations[*].property.site_area",
        "A check valuation's site area for the property.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "check_valuations[*].subject_attributes.sale_price",
        "The subject property's sale price, as a check valuation states it.",
        "money",
        _VAL,
        (),  # the twin in this slot owns these labels
        qualifiers=_MONEY_GST,
    ),
    _c(
        "check_valuations[*].subject_attributes.sale_date",
        "The date the subject property sold, as a check valuation states it.",
        "date",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "check_valuations[*].subject_attributes.gfa_nla",
        "The subject property's floor area, as a check valuation states it.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "check_valuations[*].subject_attributes.land_area",
        "The subject property's land area, as a check valuation states it.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "check_valuations[*].subject_attributes.derived_rate",
        "The subject property's rate per unit of area, per a check valuation.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "check_valuations[*].comp_set[*].sale_price",
        "A check valuation comparable's sale price.",
        "money",
        _VAL,
        (),  # the twin in this slot owns these labels
        qualifiers=_MONEY_GST,
    ),
    _c(
        "check_valuations[*].comp_set[*].sale_date",
        "The date a check valuation comparable sold.",
        "date",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "check_valuations[*].comp_set[*].gfa_nla",
        "A check valuation comparable's floor area.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "check_valuations[*].comp_set[*].land_area",
        "A check valuation comparable's land area.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    _c(
        "check_valuations[*].comp_set[*].derived_rate",
        "A check valuation comparable's rate per unit of area.",
        "number",
        _VAL,
        (),  # the twin in this slot owns these labels
    ),
    # --- schema 0.11: the capital stack (DECISIONS 2026-10-07) -------------------------
    _c(
        "economics.limit_includes_capitalised_interest",
        "Whether the facility limit includes interest that is capitalised rather than paid.",
        "bool",
        (_ITS, _LOANDOC),
        ("capitalised interest", "interest capitalised", "includes capitalised interest"),
        material=True,
    ),
    _c(
        "economics.contingency",
        "The construction contingency allowed in the project budget.",
        "money",
        (_FT, _QS),
        ("contingency", "construction contingency", "contingency allowance"),
        qualifiers=_MONEY_GST,
        authority=(_QS, _FT),
        material=True,
    ),
    _c(
        "security.lien_position",
        "Where the lender's mortgage ranks on title; 1 is first ranking.",
        "number",
        ("legal_review", _LOANDOC, "deed_of_priority"),
        ("ranking", "mortgage ranking", "priority ranking", "lien position"),
        material=True,
    ),
    _c(
        "covenants[*].basis",
        "What a covenant threshold is measured against, as the document words it.",
        "text",
        (_ITS, _LOANDOC),
        ("covenant basis", "measured against", "tested on"),
    ),
    _c(
        "valuation.gst_basis",
        "Whether the valuation states its values inclusive or exclusive of GST.",
        "text",
        _VAL,
        ("gst basis", "values are stated", "inclusive of gst", "exclusive of gst"),
        material=True,
    ),
    _c(
        "capital_stack.subordinated[*].lender",
        "The lender of a facility ranking behind the senior facility.",
        "text",
        ("deed_of_priority", _LOANDOC),
        ("subordinated lender", "second mortgagee", "mezzanine lender"),
    ),
    _c(
        "capital_stack.subordinated[*].amount",
        "The limit of a facility ranking behind the senior facility.",
        "money",
        ("deed_of_priority", _LOANDOC, _FT),
        ("subordinated facility", "mezzanine facility", "second mortgage facility"),
        qualifiers=_MONEY,
        authority=("deed_of_priority", _LOANDOC, _FT),
        material=True,
    ),
    _c(
        "capital_stack.subordinated[*].priority_amount",
        "The amount up to which the senior lender ranks first under the deed of priority.",
        "money",
        "deed_of_priority",
        ("priority amount", "senior priority amount", "first priority amount"),
        qualifiers=_MONEY,
        material=True,
    ),
    _c(
        "capital_stack.subordinated[*].standstill",
        "The period or terms during which the subordinated lender may not enforce.",
        "text",
        "deed_of_priority",
        ("standstill", "standstill period", "enforcement standstill"),
    ),
    _c(
        "capital_stack.subordinated[*].maturity",
        "The maturity date of a facility ranking behind the senior facility.",
        "date",
        ("deed_of_priority", _LOANDOC),
        ("subordinated maturity", "mezzanine maturity"),
    ),
    _c(
        "presales[*].sunset_date",
        "The date after which a presale contract may be rescinded if the lot is not registered.",
        "date",
        "presales",
        ("sunset date", "sunset clause date", "sunset"),
        material=True,
    ),
    _c(
        "presales[*].deposit.form",
        "The form the presale deposit takes: cash, deposit bond or bank guarantee, as worded.",
        "text",
        ("presales", "deposits_in_trust"),
        ("deposit form", "deposit bond", "deposit guarantee"),
    ),
    # --- schema 0.12: what signed papers cite (DECISIONS 2026-10-09) --------------------
    _c(
        "economics.fees[admin].pct",
        "The administration fee as a % of the facility.",
        "percent",
        (_ITS, _LOANDOC),
        ("admin fee", "administration fee", "admin fee (%)"),
    ),
    _c(
        "economics.fees[admin].amount",
        "The administration fee in dollars.",
        "money",
        (_ITS, _LOANDOC, _FT),
        ("admin fee amount", "administration fee ($)", "admin fee ($)"),
        qualifiers=_MONEY,
    ),
    _c(
        "economics.fees[monthly].amount",
        "A fee charged each month, in dollars per month.",
        "money",
        (_ITS, _LOANDOC),
        ("monthly fee", "monthly fees", "monthly account fee"),
        qualifiers=_MONEY,
    ),
    _c(
        "valuation.methodology.adopted_rate",
        "The rate per square metre the valuer adopts to value the subject (area x rate = value).",
        "money",
        _VAL,
        ("adopted rate", "rate adopted", "adopted rate per sqm"),
        qualifiers=_MONEY,
    ),
    _c(
        "valuation.unit_schedule[*].value",
        "The valuer's assessed value of one subject unit.",
        "money",
        _VAL,
        (),  # tabulated per unit; no label of its own
        qualifiers=_MONEY_GST,
    ),
    _c(
        "valuation.unit_schedule[*].area",
        "The internal area of one subject unit, in square metres.",
        "number",
        _VAL,
        (),
    ),
    _c(
        "valuation.unit_schedule[*].outdoor_area",
        "The private open space of one subject unit, in square metres.",
        "number",
        _VAL,
        (),
    ),
    _c(
        "valuation.unit_schedule[*].rate",
        "The assessed value of one subject unit per square metre of internal area.",
        "money",
        _VAL,
        (),
        qualifiers=_MONEY,
    ),
    # check_valuations mirrors: definition, kind and materiality only; the twin owns the labels.
    _c("check_valuations[*].report_date", "A check valuation report's issue date.", "date", _VAL, ()),
    _c(
        "check_valuations[*].methodology.adopted_rate",
        "A check valuation's adopted rate per square metre.",
        "money",
        _VAL,
        (),
        qualifiers=_MONEY,
    ),
    _c(
        "check_valuations[*].unit_schedule[*].value",
        "A check valuation's assessed value of one subject unit.",
        "money",
        _VAL,
        (),
        qualifiers=_MONEY_GST,
    ),
    _c(
        "check_valuations[*].unit_schedule[*].area",
        "A check valuation's unit internal area.",
        "number",
        _VAL,
        (),
    ),
    _c(
        "check_valuations[*].unit_schedule[*].outdoor_area",
        "A check valuation's unit private open space.",
        "number",
        _VAL,
        (),
    ),
    _c(
        "check_valuations[*].unit_schedule[*].rate",
        "A check valuation's unit value per square metre.",
        "money",
        _VAL,
        (),
        qualifiers=_MONEY,
    ),
)


def _build(concepts: tuple[Concept, ...]) -> Mapping[str, Concept]:
    registry: dict[str, Concept] = {}
    for concept in concepts:
        if concept.path in registry:
            raise ValueError(f"concept {concept.path} is defined twice")
        registry[concept.path] = concept
    return MappingProxyType(registry)


REGISTRY: Mapping[str, Concept] = _build(_CONCEPTS)


def registry_digest() -> str:
    """A stable digest of the whole registry, pinned per ``REGISTRY_VERSION`` by a test."""
    rows = [asdict(REGISTRY[path]) for path in sorted(REGISTRY)]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode("utf-8")).hexdigest()


_KEY = re.compile(r"\[([^\]]*)\]")


def pattern_of(path: str) -> str:
    """``path`` with every collection key replaced by the wildcard."""
    return _KEY.sub(f"[{WILDCARD}]", path)


def concept_for(path: str) -> Concept | None:
    """The concept a concrete field path (or a pattern) belongs to, or None.

    A fixed-key concept (``valuation.values_by_basis[as_is]``) wins over a wildcard
    one; among candidates, the one with the fewest wildcards wins.
    """
    exact = REGISTRY.get(path)
    if exact is not None:
        return exact
    keys = _KEY.findall(path)
    if not keys:
        return None
    parts = _KEY.split(path)  # text, key, text, key, ..., text
    best: Concept | None = None
    best_wild = len(keys) + 1
    for choice in product((False, True), repeat=len(keys)):
        wild = sum(choice)
        if wild >= best_wild:
            continue
        out = [parts[0]]
        for i, use_wildcard in enumerate(choice):
            out.append(f"[{WILDCARD if use_wildcard else keys[i]}]")
            out.append(parts[2 * i + 2])
        found = REGISTRY.get("".join(out))
        if found is not None:
            best, best_wild = found, wild
    return best


def _alias_index() -> Mapping[tuple[str, str], tuple[Concept, ...]]:
    index: dict[tuple[str, str], list[Concept]] = {}
    for concept in REGISTRY.values():
        for slot in concept.sources:
            for alias in concept.aliases:
                index.setdefault((slot, alias), []).append(concept)
    return MappingProxyType({k: tuple(v) for k, v in index.items()})


_ALIASES = _alias_index()


def resolve_label(label: str, slot: str) -> Concept | None:
    """The single concept a document label names in a slot's documents, or None.

    None when the label matches no alias of a concept that lists ``slot`` as a source,
    or matches more than one. A figure with no single concept is unmapped and goes to
    review; it is never guessed.
    """
    matches = _ALIASES.get((slot, normalise_label(label)), ())
    return matches[0] if len(matches) == 1 else None


def _fact_arg(annotation: Any) -> Any:
    meta = getattr(annotation, "__pydantic_generic_metadata__", None) or {}
    args = meta.get("args") or ()
    return args[0] if args else None


def _is_fact(annotation: Any) -> bool:
    origin = (getattr(annotation, "__pydantic_generic_metadata__", None) or {}).get("origin") or annotation
    return isinstance(origin, type) and issubclass(origin, Fact)


def fact_type(path: str) -> Any:
    """The declared value type of the ``Fact`` a path (or pattern) names, or None.

    Walks the model's declared types, not an instance, so keyed collections resolve on
    an empty ``DealModel`` (a key, or the wildcard, selects the dict's value type).
    None when the path does not name a ``Fact`` field.
    """
    from creditpaper.model.deal import DealModel

    annotation: Any = DealModel
    for segment in path.split("."):
        name, _, key = segment.partition("[")
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)) or _is_fact(annotation):
            return None
        field = annotation.model_fields.get(name)
        if field is None:
            return None
        annotation = field.annotation
        if key:
            if get_origin(annotation) is not dict:
                return None
            annotation = get_args(annotation)[1]
    return _fact_arg(annotation) if _is_fact(annotation) else None


def qualified_fact(concept: Concept, fact: Fact[Any], basis: FactBasis) -> Fact[Any]:
    """The fact as the commit path writes it: its basis, and the registry version.

    - A concept that requires ``GST_BASIS`` must be committed with ``basis.gst`` set.
      ``GstBasis.UNSTATED`` (read, and the span states none) is allowed; None (never
      read) is refused.
    - ``GstBasis.MIXED`` on a material money concept flags an extracted fact: the span
      states both bases, and which one the figure is on is a human's call.
    - ``provenance.registry_version`` records the registry the fact was mapped under.
    """
    if Qualifier.GST_BASIS in concept.qualifiers and basis.gst is None:
        raise ValueError(f"{concept.path} requires a GST basis; record UNSTATED when the span states none")
    update: dict[str, Any] = {"basis": basis}
    if fact.provenance is not None:
        update["provenance"] = fact.provenance.model_copy(update={"registry_version": REGISTRY_VERSION})
    if (
        concept.material
        and concept.kind == "money"
        and basis.gst is GstBasis.MIXED
        and fact.status is FactStatus.EXTRACTED
    ):
        update["status"] = FactStatus.FLAGGED
    return fact.model_copy(update=update)
