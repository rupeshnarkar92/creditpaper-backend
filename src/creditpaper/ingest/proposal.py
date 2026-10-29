"""Propose one slot per document, with its evidence and an honest outcome.

Three findings from the fixture corpus shape this module.

First, the deterministic signals settle 51% of documents at 82% accuracy. That
is not a filing system, so the model proposes for everything they do not settle
- 50-65% of documents - reading only the first pages. Where two signals already
agree, the model is skipped: agreement measured 82% on its own and the call buys
little.

Second, given 148 documents belonging in no slot at all, the stack still filed
75.7% of them. NOT_A_DEAL_DOCUMENT is therefore a real outcome with its own
floor, not an absence of one. Filing a warranty card as a valuation is worse
than admitting the document is not part of the deal.

Third, borrower and builder financials (and their ATO statements) are provably
not separable from the document alone: the axis is which party the document
belongs to, which the document does not state in any consistent way. Those slots
return UNKNOWN with needs_party_context until the signed ITS supplies the names.

The model call goes through llm.client.messages_create with the model chosen by
llm.registry for Task.CLASSIFY. A provider failure degrades the proposal to
UNKNOWN; intake must not stop because one classifier is down.
"""

from __future__ import annotations

import json
import logging
from enum import Enum

from pydantic import BaseModel

from creditpaper.ingest.intake_store import labelled_corpus
from creditpaper.ingest.signals import PARTY_SENSITIVE_SLOTS, Signal, all_signals
from creditpaper.llm.client import messages_create
from creditpaper.llm.registry import Task, resolve

log = logging.getLogger(__name__)

AGREEMENT_CONFIDENCE = 0.82
MODEL_FLOOR = 0.50
# Classification reads the opening pages and nothing else, so this bound costs
# no facts: it decides which slot a document belongs to, never what it says.
# Extraction is bounded by planned windows instead, and never truncated.
_MAX_PROMPT_CHARS = 6000

_SYSTEM = (
    "You file documents into a lender's credit-paper slots. Read the opening pages and answer with "
    'JSON only: {"outcome": "slot"|"not_a_deal_document"|"unknown", "slot_key": string|null, '
    '"confidence": 0-1, "reason": string}. Choose not_a_deal_document for anything that does not '
    "change the credit decision - handover and maintenance manuals, warranty cards, invoices, "
    "drawings, marketing collateral. Choose unknown when the pages do not settle it. Do not guess "
    "between two slots; unknown is the correct answer when you are unsure."
)


class ProposalOutcome(str, Enum):
    SLOT = "slot"
    NOT_A_DEAL_DOCUMENT = "not_a_deal_document"
    UNKNOWN = "unknown"


class Evidence(BaseModel):
    signal: str
    slot_key: str | None
    score: float
    detail: str


class SlotProposal(BaseModel):
    outcome: ProposalOutcome
    slot_key: str | None
    confidence: float
    evidence: list[Evidence]
    needs_party_context: bool = False


def _as_evidence(signals: list[Signal]) -> list[Evidence]:
    return [Evidence(signal=s.signal, slot_key=s.slot_key, score=s.score, detail=s.detail) for s in signals]


def _agreed_slot(signals: list[Signal]) -> str | None:
    votes: dict[str, int] = {}
    for signal in signals:
        if signal.slot_key:
            votes[signal.slot_key] = votes.get(signal.slot_key, 0) + 1
    if not votes:
        return None
    best = max(votes.items(), key=lambda kv: kv[1])
    return best[0] if best[1] >= 2 else None


def _ask_model(filename: str, path: str, first_pages: dict[int, str]) -> tuple[SlotProposal | None, Evidence]:
    choice = resolve(Task.CLASSIFY)
    body = " ".join(first_pages[n] for n in sorted(first_pages))[:_MAX_PROMPT_CHARS]
    prompt = f"Filename: {filename}\nFolder: {path}\n\nOpening pages:\n{body}"
    try:
        response = messages_create(
            tool=f"ingest:classify:{choice.provider}",
            stage="extract",
            provider=choice.provider,
            model=choice.model,
            max_tokens=300,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        # Reading the answer sits inside the same guard as making the call. A
        # classifier that returns nonsense - a JSON body that is not JSON, an
        # outcome that is not one of ours, a confidence that is not a number -
        # is the same event to the analyst as one that is down, and neither may
        # stop the rest of the folder.
        payload = json.loads(response.text)
        if not isinstance(payload, dict):
            raise ValueError("answer was not a JSON object")
        outcome = ProposalOutcome(str(payload.get("outcome", "unknown")))
        slot_key = payload.get("slot_key") or None
        confidence = float(payload.get("confidence") or 0.0)
        reason = str(payload.get("reason") or "model proposal")
    except Exception as exc:  # provider down, malformed answer: degrade, never stop intake
        log.warning("slot classification unavailable: %s", exc)
        return None, Evidence(
            signal="model", slot_key=None, score=0.0, detail=f"classifier unavailable: {exc}"
        )

    evidence = Evidence(signal="model", slot_key=slot_key, score=confidence, detail=reason)
    if outcome is ProposalOutcome.SLOT and confidence < MODEL_FLOOR:
        outcome, slot_key = ProposalOutcome.UNKNOWN, None
    if outcome is not ProposalOutcome.SLOT:
        # A slot key only means something alongside a SLOT outcome. An answer
        # naming a slot while saying "unknown" - or omitting the outcome
        # entirely - must not leave that key on the proposal, or the stored row
        # reads as a filing nobody proposed. The key stays visible in evidence.
        slot_key = None
    return (
        SlotProposal(outcome=outcome, slot_key=slot_key, confidence=confidence, evidence=[]),
        evidence,
    )


def _gate_party_context(proposal: SlotProposal, party_names: dict[str, str] | None) -> SlotProposal:
    if proposal.slot_key in PARTY_SENSITIVE_SLOTS and not party_names:
        return proposal.model_copy(
            update={
                "outcome": ProposalOutcome.UNKNOWN,
                "slot_key": None,
                "needs_party_context": True,
                "confidence": 0.0,
            }
        )
    return proposal


def propose_slot(
    tenant_id: str,
    deal_id: str,
    *,
    content_hash: str,
    filename: str,
    path: str,
    first_pages: dict[int, str],
    party_names: dict[str, str] | None = None,
    labelled: list[tuple[str, str]] | None = None,
) -> SlotProposal:
    # The corpus is the same for every document in a folder, and shingling it is
    # the expensive part: a folder of 300 files was 300 queries returning up to
    # 500 documents' text each. A batch caller fetches it once and passes it in;
    # a single-document caller still gets the convenience of not having to.
    corpus = labelled_corpus(tenant_id) if labelled is None else labelled
    signals = all_signals(filename=filename, path=path, first_pages=first_pages, labelled=corpus)
    evidence = _as_evidence(signals)

    agreed = _agreed_slot(signals)
    if agreed:
        return _gate_party_context(
            SlotProposal(
                outcome=ProposalOutcome.SLOT,
                slot_key=agreed,
                confidence=AGREEMENT_CONFIDENCE,
                evidence=evidence,
                needs_party_context=False,
            ),
            party_names,
        )

    model_proposal, model_evidence = _ask_model(filename, path, first_pages)
    evidence.append(model_evidence)
    if model_proposal is None:
        return SlotProposal(outcome=ProposalOutcome.UNKNOWN, slot_key=None, confidence=0.0, evidence=evidence)
    return _gate_party_context(model_proposal.model_copy(update={"evidence": evidence}), party_names)
