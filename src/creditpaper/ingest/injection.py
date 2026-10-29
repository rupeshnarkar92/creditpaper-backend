"""Screen text facts for instructions aimed at a model (spec §3.9, schema 0.11).

Deal documents are untrusted input, and their text reaches prompts: as fact
values, as the clause a fact was read from, and through the writers. The
generator already treats every fact as data (`generation/prefix.py`); this
screen says which facts LOOK written to steer a model, so the review surface can
show it and the pipeline can keep them out of anything instruction-shaped.

Deterministic and deliberately narrow. Credit documents are full of
"instructions", "systems", "users" and things to "disregard", so every pattern
here needs a model-directed shape: an override of earlier instructions, talk of
a system prompt, an address to an AI, a chat role marker, one of our own prompt
tags. A miss is caught downstream (facts are data); a false alarm costs an
analyst's attention on every deal, so the benign table in the tests is the bar.

Applied when the deal is read (`ledger.service.load_model`), like the evidence
tier: it is a pure function of the stored value and clause, so storing it would
only change fingerprints. It sets the flag and nothing else: status and
confidence are untouched, because a strange clause is not a wrong figure.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from creditpaper.model.evidence import model_facts

__all__ = ["annotate_injection", "injection_suspected"]

_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE | re.MULTILINE)
    for p in (
        # "ignore all previous instructions", "disregard the above instructions"
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}"
        r"\b(previous|prior|above|earlier|preceding|all|any|your)\b[^.\n]{0,20}"
        r"\b(instructions?|prompts?|directions|rules)\b",
        # talk of the model's own instructions
        r"\b(system|developer)\s+(prompt|message)\b",
        r"\bnew\s+instructions?\s*:",
        # an address to a model
        r"\byou\s+are\s+(now\s+)?(an?|the)\s+(ai|assistant|language\s+model|chatbot|llm)\b",
        r"\bas\s+an?\s+(ai|large\s+language\s+model|language\s+model)\b",
        r"\b(llm|chatgpt|large\s+language\s+model|ai\s+(model|assistant))\b[^.\n]{0,30}\b(reading|processing|reviewing)\b",
        r"\bnote\s+to\s+the\s+(ai|llm|model|assistant)\b",
        # a chat role marker opening a line
        r"^\s*(assistant|system)\s*:",
        # one of our own prompt tags, opened or closed
        r"</?\s*(fact_base|system|instructions?|context|span|assistant)\s*>",
    )
)


def injection_suspected(text: str) -> bool:
    """True when `text` reads as instructions aimed at a model."""
    return any(p.search(text) for p in _PATTERNS)


def annotate_injection(model: BaseModel) -> None:
    """Flag, in place, every TEXT fact whose value or clause reads as an instruction to a model.

    Numbers, dates and booleans are never screened: the value cannot carry an
    instruction, and its clause is a figure's context, not free text a writer
    restates. An unflagged fact keeps `None`, so its dump is unchanged."""
    for fact in model_facts(model):
        if not isinstance(fact.value, str):
            continue
        clause = fact.provenance.location if fact.provenance is not None else ""
        fact.injection_suspected = (
            True if injection_suspected(fact.value) or injection_suspected(clause) else None
        )
