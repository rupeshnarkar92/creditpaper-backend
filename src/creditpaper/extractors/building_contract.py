"""Building-contract extractor — the construction agreement (3.11; checks #3/#14).

The signed building contract fixes the price and risk allocation for the build: the
contract sum (vs the funding-table construction line and the QS view), the contract
type (lump-sum transfers overrun risk to the builder; cost-plus keeps it with the
borrower), retention, defects liability, the programme, and the material clauses
(liquidated damages, variations, termination). Material terms only (D7) — not the
full conditions of contract. Each fact carries its page + clause (D4).
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
from creditpaper.model.core import Fact

__all__ = ["Anchored", "BuildingContractExtraction", "BuildingContractExtractor", "ClauseOut"]


class ClauseOut(BaseModel):
    summary: Anchored | None = None  # a material clause, restated concisely


class BuildingContractExtraction(BaseModel):
    contract_type: Anchored | None = None  # lump-sum / cost-plus / design & construct
    contract_sum: Anchored | None = None
    retention_amount: Anchored | None = None
    retention_timing: Anchored | None = None
    defects_liability_period: Anchored | None = None
    programme: Anchored | None = None  # construction period / practical completion date
    key_clauses: list[ClauseOut] = []  # LDs, variations, termination, PC sums
    tripartite_present: Anchored | None = None  # "true"/"false": a tripartite / step-in deed exists
    step_in_rights: Anchored | None = None  # the lender's step-in rights, restated concisely


_INSTRUCTION = (
    "You are extracting the CREDIT-MATERIAL terms from a signed building/construction "
    "contract (e.g. HIA, ABIC, or a bespoke D&C contract) for a CRE construction loan. "
    "Extract ONLY what the contract states; for EVERY value record the page and a verbatim clause as its "
    "anchor; OMIT anything you cannot locate (no anchor, no "
    "field). Numbers as plain digits (no $, commas). Capture: the CONTRACT TYPE "
    "(lump-sum / fixed-price, cost-plus, or design & construct — this allocates overrun "
    "risk); the CONTRACT SUM; the RETENTION (amount or %, and when released); the "
    "DEFECTS LIABILITY PERIOD; the PROGRAMME (construction period or practical-completion "
    "date); and the MATERIAL clauses only — liquidated damages, variations regime, "
    "provisional/PC sums, and termination. Also capture whether a TRIPARTITE / step-in "
    "deed (a side deed among lender, developer and builder, sometimes called a collateral "
    "warranty) is referenced (tripartite_present true/false) and, if so, the LENDER'S "
    "STEP-IN RIGHTS on builder default, restated concisely (step_in_rights) — a credit "
    "mitigant. Skip boilerplate conditions. Return ONLY a JSON object of this shape, "
    "omitting fields you cannot find. For retention_amount give the cap as a plain number "
    "where stated (put the % and mechanics in retention_timing):\n"
    '{"contract_type": {"value": "Lump-sum fixed-price", "page": 1, "clause": "The contract is a Lump-sum '
    'fixed-price agreement in the ABIC SW-2018 form."},\n'
    ' "contract_sum": {"value": "6172046", "page": 2, "clause": "The Contract Sum is 6,172,046 including GST '
    'and all provisional sums."},\n'
    ' "retention_amount": {"value": "308602", "page": 5, "clause": "Retention of 5% applies to each payment '
    'claim and is capped at 308,602."},\n'
    ' "retention_timing": {"value": "5%; half released at PC, half after defects liability", '
    '"page": 5, "clause": "..."},\n'
    ' "defects_liability_period": {"value": "12 months from practical completion", "page": 6, '
    '"clause": "..."},\n'
    ' "programme": {"value": "52 weeks", "page": 3, "clause": "The works must reach practical completion '
    'within 52 weeks of site possession."},\n'
    ' "key_clauses": [{"summary": {"value": "liquidated damages $2,000/day", "page": 4, '
    '"clause": "..."}}],\n'
    ' "tripartite_present": {"value": "true", "page": 8, "clause": "The parties have entered a tripartite '
    'deed granting the financier step-in rights."},\n'
    ' "step_in_rights": {"value": "lender may step in and novate the contract on builder '
    'default, on 10 business days notice", "page": 8, "clause": "..."}}'
    "\n" + ANCHOR_RULE
)


class BuildingContractExtractor(Extractor):
    slot_key = "building_contract"

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name

    def _extract_raw(
        self,
        pdf_b64: str | None = None,
        *,
        png_pages: list[str] | None = None,
        text_context: str = "",
    ) -> BuildingContractExtraction:
        if png_pages or text_context:
            return validate_extraction(
                BuildingContractExtraction,
                document_json_pages(
                    png_pages,
                    _INSTRUCTION,
                    text_context=text_context,
                    model_name=self._model_name,
                    tool="extract:building_contract",
                ),
            )
        return validate_extraction(
            BuildingContractExtraction,
            document_json(pdf_b64 or "", _INSTRUCTION, self._model_name, tool="extract:building_contract"),
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
            _idx = select_pages(page_texts, "building_contract")
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

        put("building_contract.contract_type", data.contract_type)
        put("building_contract.contract_sum", data.contract_sum)
        put("building_contract.retention.amount", data.retention_amount)
        put("building_contract.retention.timing", data.retention_timing)
        put("building_contract.defects_liability_period", data.defects_liability_period)
        put("building_contract.programme", data.programme)
        for c in data.key_clauses:
            if c.summary and c.summary.value:
                note(f"building_contract.key_clauses[{safe_slug(str(c.summary.value)[:48])}]", c.summary)
        put("building_contract.step_in_rights", data.step_in_rights)
        add_bool_fact(
            fields, flags, document_id, "building_contract.tripartite_present", data.tripartite_present
        )
        return ExtractionOutput(fields=fields, flags=flags)
