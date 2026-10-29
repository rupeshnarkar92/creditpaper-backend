"""Planning-approval extractor — Instance D, MATERIAL-ONLY, JURISDICTION-NEUTRAL.

A development is authorised differently in each Australian state (NSW development
consent / Notice of Determination; VIC planning permit + endorsed plans; QLD
decision notice / development approval; WA development approval; SA development
approval via Decision Notification Form). Rather than one extractor per state, this
reads the CREDIT CONCEPTS that every regime expresses and lets the model map the
local instrument onto them (research, DECISIONS.md 2026-06-16). It takes only what
validates the credit (D7): is there a valid, operative, build-ready approval; is it
current; what does it approve; which conditions bear on cost/time/drawdown; and the
downstream build/occupation authorisations. It does NOT transcribe the full
condition schedule.

Facts that no jurisdiction reliably prints on the approval — whether it has actually
commenced/lapsed, the operative version, build/occupation status, and contribution $
that sits on a separate notice — are captured as best they can and otherwise left to
scaffold to an analyst-confirm (feeding check #13), in every state alike.

Each fact carries its page + clause (D4). Output flows through the apply layer.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from creditpaper.extractors._doc import (
    ANCHOR_RULE,
    Anchored,
    add_bool_fact,
    add_fact,
    add_note_fact,
    b64,
    document_json,
    document_json_pages,
    validate_extraction,
)
from creditpaper.extractors.base import ExtractionOutput, Extractor
from creditpaper.model.citations import safe_slug
from creditpaper.model.core import ExtractionMethod, Fact, FactStatus, Provenance

__all__ = [
    "Anchored",
    "DaConditionOut",
    "DaContributionOut",
    "DaExtraction",
    "DaExtractor",
    "DownstreamOut",
]


class DaConditionOut(BaseModel):
    summary: Anchored | None = None  # the condition, restated concisely (-> da.conditions[k].text)
    status: Anchored | None = None  # satisfied / outstanding / deferred / ongoing
    blocker: bool = False  # must be satisfied before commencement or occupation


class DaContributionOut(BaseModel):
    description: Anchored | None = None  # e.g. "infrastructure contribution", "open space levy"
    amount: Anchored | None = None  # where a $ is stated ON the approval
    trigger: Anchored | None = None  # when payable
    on_separate_notice: bool = False  # the $ lives on a separate notice (QLD ICN, VIC DCP, WA formula)


class DownstreamOut(BaseModel):
    kind: Anchored | None = None  # named as the jurisdiction names it
    reference: Anchored | None = None
    status: Anchored | None = None  # issued / not yet issued / unknown


class DaExtraction(BaseModel):
    instrument_type: Anchored | None = None
    reference: Anchored | None = None
    authority: Anchored | None = None
    approved_date: Anchored | None = None  # ISO YYYY-MM-DD
    approved_description: Anchored | None = None
    approved_use: Anchored | None = None
    build_ready: Anchored | None = None  # "true"/"false": operative & authorises commencement
    build_ready_basis: Anchored | None = None
    lapse_date: Anchored | None = None  # ISO, where a date is stated
    lapse_basis: Anchored | None = None  # the currency RULE as stated
    approval_current: Anchored | None = None  # "true"/"false": in force / not lapsed
    material_conditions: list[DaConditionOut] = []
    contributions: list[DaContributionOut] = []
    build_authorisation: DownstreamOut | None = None
    occupation_authorisation: DownstreamOut | None = None


_INSTRUCTION = (
    "You are extracting the CREDIT-MATERIAL facts from an Australian planning approval "
    "supporting a CRE construction loan. The document may be from ANY state, so read by "
    "CONCEPT, not by one state's labels. The primary approval is called: a development "
    "consent / Notice of Determination (NSW), a planning permit + endorsed plans (VIC), "
    "a decision notice / development approval (QLD), or a development approval / Decision "
    "Notification Form (WA, SA). Extract ONLY what the document states; for EVERY value "
    "record the page and a verbatim clause as its anchor; OMIT anything you cannot "
    "locate (no anchor, no field). Dates as ISO YYYY-MM-DD. Do NOT assume NSW specifics "
    "(no fixed '5 year' lapse, no 's7.11', no 'Construction Certificate') — read what "
    "THIS instrument says.\n"
    "Capture:\n"
    "- instrument_type: what kind of approval it is, as named.\n"
    "- reference: the approval/application identifier; authority: who granted it; "
    "approved_date.\n"
    "- approved_description and approved_use: what may be built and the use.\n"
    "- build_ready (true/false) + build_ready_basis: is this an OPERATIVE approval that "
    "authorises the development to PROCEED? It is NOT build-ready if it is a NSW deferred-"
    "commencement consent with conditions unsatisfied, a VIC Notice of Decision (not yet "
    "a permit), a QLD preliminary approval (not a development permit), or an SA planning "
    "consent without building consent. State the basis in the instrument's words.\n"
    "- lapse_date (if a date is stated) and lapse_basis: the currency/expiry RULE as "
    "stated (the period and what starts/extends it) — quote it, do not assume a period. "
    "approval_current (true/false) only if the document itself states it is in force.\n"
    "- material_conditions: ONLY conditions that bear on the credit — conditions "
    "precedent to commencement (incl. deferred commencement / 'before development "
    "starts'), conditions precedent to occupation/title, and staging. Mark blocker=true "
    "where it gates commencement or occupation. Skip routine construction-management, "
    "hours, waste and administrative conditions.\n"
    "- contributions: monetary obligations (developer/infrastructure/open-space "
    "contribution, levy, bond). Give the amount ONLY if a $ is stated on this document; "
    "if the document says the amount is set by a separate notice/plan/schedule (e.g. an "
    "infrastructure charges notice or contributions plan), set on_separate_notice=true "
    "and omit amount.\n"
    "- build_authorisation and occupation_authorisation: the SEPARATE downstream "
    "approvals to physically build and to occupy (Construction/Occupation Certificate, "
    "building permit/occupancy permit, building development approval / certificate of "
    "occupancy, building consent). Record kind + reference + status only if this "
    "document states them; otherwise omit (they are usually separate instruments).\n"
    "Return ONLY a JSON object of this shape, omitting fields you cannot find:\n"
    '{"instrument_type": {"value": "Development Consent (Notice of Determination)", '
    '"page": 1, "clause": "..."},\n'
    ' "reference": {"value": "DA-100/2021", "page": 1, "clause": "Application No. DA-100/2021 was determined '
    'by the consent authority as set out below."},\n'
    ' "authority": {"value": "Waverley Council", "page": 1, "clause": "This consent is granted by Waverley '
    'Council as the consent authority for the land."},\n'
    ' "approved_date": {"value": "2021-08-19", "page": 1, "clause": "The determination date of this '
    'development consent is 19 August 2021."},\n'
    ' "approved_description": {"value": "demolition and construction of 2 dwellings", '
    '"page": 1, "clause": "..."},\n'
    ' "approved_use": {"value": "residential", "page": 1, "clause": "Consent is granted for residential '
    'development comprising attached dwellings on the land."},\n'
    ' "build_ready": {"value": "true", "page": 2, "clause": "This consent became operative on the date '
    'endorsed and has not since lapsed."},\n'
    ' "lapse_basis": {"value": "lapses 5 years from the date the consent operates", '
    '"page": 2, "clause": "..."},\n'
    ' "material_conditions": [{"summary": {"value": "contribution payable prior to '
    'Construction Certificate", "page": 5, "clause": "Condition 22 requires the contribution to be paid '
    'prior to the Construction Certificate."}, "status": '
    '{"value": "outstanding", "page": 5, "clause": "..."}, "blocker": true}],\n'
    ' "contributions": [{"description": {"value": "s7.11 contribution", "page": 5, '
    '"clause": "Condition 22 imposes a s7.11 contribution payable to the council."}, "amount": {"value": '
    '"128400", "page": 5, "clause": "A contribution of 128,400 is payable under Condition 22 before works '
    'commence."}, '
    '"trigger": {"value": "prior to Construction Certificate", "page": 5, "clause": "The contribution must '
    'be paid prior to Construction Certificate issue for any stage."}, '
    '"on_separate_notice": false}],\n'
    ' "build_authorisation": {"kind": {"value": "Construction Certificate", "page": 1, '
    '"clause": "..."}, "status": {"value": "not yet issued", "page": 1, "clause": "..."}}}'
    "\n" + ANCHOR_RULE
)


class DaExtractor(Extractor):
    slot_key = "da"

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name

    # -- LLM/document boundary (override in tests) ---------------------------
    def _extract_raw(
        self,
        pdf_b64: str | None = None,
        *,
        png_pages: list[str] | None = None,
        text_context: str = "",
    ) -> DaExtraction:
        if png_pages or text_context:
            return validate_extraction(
                DaExtraction,
                document_json_pages(
                    png_pages,
                    _INSTRUCTION,
                    text_context=text_context,
                    model_name=self._model_name,
                    max_tokens=12000,
                    tool="extract:da",
                ),
            )
        return validate_extraction(
            DaExtraction,
            document_json(pdf_b64 or "", _INSTRUCTION, self._model_name, max_tokens=12000, tool="extract:da"),
        )

    def extract(
        self,
        document_id: str,
        path: Path,
        *,
        page_texts: list[dict] | None = None,
    ) -> ExtractionOutput:
        from creditpaper.extractors._doc import pages_to_png
        from creditpaper.processing.page_selector import select_pages

        if page_texts:
            _idx = select_pages(page_texts, "da")
            _pngs = pages_to_png(path, _idx)
            _ctx = "\n\n".join(
                f"[Page {pt['page']}]\n{pt.get('text', '')}"
                for pt in page_texts
                if (pt.get("page", 1) - 1) in _idx
            )
            data = self._extract_raw(png_pages=_pngs, text_context=_ctx)
        else:
            data = self._extract_raw(pdf_b64=b64(path))
        fields: dict[str, Fact] = {}
        flags: list[str] = []

        def put(p: str, anc: Anchored | None) -> None:
            add_fact(fields, flags, document_id, p, anc)

        def note(p: str, anc: Anchored | None) -> None:
            add_note_fact(fields, flags, document_id, p, anc)

        put("da.instrument_type", data.instrument_type)
        put("da.reference", data.reference)
        put("da.authority", data.authority)
        put("da.approved_date", data.approved_date)
        put("da.approved_description", data.approved_description)
        put("da.approved_use", data.approved_use)
        note("da.build_ready_basis", data.build_ready_basis)
        put("da.lapse_date", data.lapse_date)
        put("da.lapse_basis", data.lapse_basis)
        add_bool_fact(fields, flags, document_id, "da.build_ready", data.build_ready)
        add_bool_fact(fields, flags, document_id, "da.approval_current", data.approval_current)

        for cond in data.material_conditions:
            if cond.summary is None or cond.summary.value is None:
                continue
            base = f"da.conditions[{safe_slug(str(cond.summary.value)[:48])}]"
            note(f"{base}.text", cond.summary)
            put(f"{base}.status", cond.status)
            if cond.blocker:
                _bool_at(fields, document_id, f"{base}.blocker", True, cond.summary)

        for con in data.contributions:
            if con.description is None or con.description.value is None:
                continue
            base = f"da.contributions[{safe_slug(str(con.description.value)[:48])}]"
            put(f"{base}.description", con.description)
            put(f"{base}.amount", con.amount)
            put(f"{base}.trigger", con.trigger)
            if con.on_separate_notice:
                _bool_at(fields, document_id, f"{base}.amount_on_separate_notice", True, con.description)

        _downstream(fields, flags, document_id, "da.build_authorisation", data.build_authorisation)
        _downstream(fields, flags, document_id, "da.occupation_authorisation", data.occupation_authorisation)
        return ExtractionOutput(fields=fields, flags=flags)


def _bool_at(fields: dict[str, Fact], document_id: str, path: str, value: bool, anchor: Anchored) -> None:
    """A typed bool fact anchored to a related clause (the condition / contribution)."""
    from creditpaper.extractors._doc import _scored

    # Scored on the clause it is anchored to, like every other fact: the bool is
    # this extractor's reading of that clause, so the clause is what is checked.
    confidence, flagged = _scored(anchor.value, anchor, path)
    fields[path] = Fact(
        value=value,
        status=FactStatus.FLAGGED if flagged else FactStatus.EXTRACTED,
        confidence=confidence,
        provenance=Provenance(
            document_id=document_id,
            page=anchor.page,
            location=anchor.clause or "clause",
            method=ExtractionMethod.LLM_EXTRACTION,
        ),
    )


def _downstream(
    fields: dict[str, Fact], flags: list[str], document_id: str, base: str, d: DownstreamOut | None
) -> None:
    if d is None:
        return
    add_fact(fields, flags, document_id, f"{base}.kind", d.kind)
    add_fact(fields, flags, document_id, f"{base}.reference", d.reference)
    add_fact(fields, flags, document_id, f"{base}.status", d.status)
