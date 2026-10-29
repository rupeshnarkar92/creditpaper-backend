"""Whole-paper proofreading pass — spelling, grammar, punctuation QC.

A final quality-control sweep over the generated prose. It is deliberately NARROW:
it fixes language only and may never touch meaning, numbers, money, dates, or the
citation markers the renderer resolves. Two guards enforce that: (1) the model is
told to return minimal {before, after} corrections, language only; (2) a
DETERMINISTIC check rejects any correction whose digit/symbol content differs from
the original (a typo fix that changes a figure or a [3] citation is not a typo fix).
Every applied change is logged, so the pass is auditable — consistent with the
findings-not-silent-rewrite posture of the fidelity gate. Proofreading itself
is mechanical, but the tier is Sonnet 5, not Haiku: Haiku 4.5 returns a 400 on
`inference_geo`, so leaving this pass on Haiku would make it the one call that
could not be US-pinned with the rest. Note `inference_geo` pins WHERE inference
runs; it is not a data-residency guarantee, and nothing here should be described
as one.
"""

from __future__ import annotations

import re
from concurrent.futures import as_completed
from dataclasses import dataclass, field

from creditpaper.agents.jsonio import iter_json_objects
from creditpaper.concurrency import pool
from creditpaper.config import proofread_model
from creditpaper.model.core import ProseBlock

# Runs of digits with their adjacent money/percent/separators, and [n] citation
# markers — the content a proofreader must leave byte-for-byte intact.
# The LEADING SIGN matters: dropping the '-' on a negative LVR headroom turns a
# covenant BREACH into headroom (the proofreader did exactly this on the Wattlefield
# blind run, 2026-06-16, because the guard compared digits but not sign).
_NUM = re.compile(r"\[\d+\]|[-+]?\$?\d[\d,.]*%?")

# Words that carry as much arithmetic as the digits beside them. A guard that
# compares only digit runs waves through "$5 million" -> "$5 billion", "May 2024"
# -> "March 2024", and "is not fundable" -> "is fundable" — each a single-word
# edit that changes the meaning of a credit paper completely while every figure in
# the text stays byte-identical.
#
# The proofreader is instructed to fix spelling and grammar only, which is exactly
# why this needs to be a mechanical guard rather than a trusted instruction: the
# whole design of this stage is that its output is checked, not believed.
_SENSE = re.compile(
    r"\b("
    r"hundred|thousand|million|billion|trillion|[km]|bn|mn"  # magnitude
    r"|january|february|march|april|may|june|july|august|september|october|november|december"
    r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
    r"|not|no|never|none|nor|without|excluding|except|less|fewer|below|under"  # negation / direction
    r"|above|over|more|greater|exceeds|exceeding|at least|at most|minimum|maximum"
    r"|per\s+annum|p\.?a\.?|per\s+month|monthly|annually"  # rate periods
    r")\b",
    re.IGNORECASE,
)


def _figures(text: str) -> list[str]:
    """Everything a proofreader must leave byte-for-byte intact.

    Digit runs AND the sense-bearing words around them, because "$5 million" and
    "$5 billion" contain identical figures.
    """
    return sorted(m.group(0) for m in _NUM.finditer(text)) + sorted(
        m.group(0).lower() for m in _SENSE.finditer(text)
    )


_INSTRUCTION = (
    "You are a meticulous proofreader for an institutional credit paper. Correct ONLY "
    "spelling, grammar, punctuation, and obvious typos. You may NOT change meaning, "
    "tone, or wording choices that are already correct; you may NOT add or remove "
    "facts; and you must leave EVERY number, dollar amount, percentage, date, proper "
    "noun, and bracketed citation marker like [3] exactly as written. Do not touch "
    "lines that are Markdown structure (starting with '#', '|', '>>', or '[ANALYST]'). "
    "Return ONLY a JSON object listing minimal corrections (the smallest substring that "
    "changes), or an empty list if the text is clean:\n"
    '{"corrections": [{"before": "the the borrower", "after": "the borrower", '
    '"kind": "grammar"}, {"before": "asssesment", "after": "assessment", '
    '"kind": "spelling"}]}\n'
    "Each 'before' must be copied verbatim from the text so it can be located. Keep "
    "each correction local (a few words), not a whole sentence."
)


@dataclass
class Correction:
    section: int
    before: str
    after: str
    kind: str


@dataclass
class ProofreadFailure:
    section: int
    error_type: str
    message: str


@dataclass
class ProofreadReport:
    applied: list[Correction] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # changed a figure/citation → refused
    # A section whose LLM call raised (e.g. TruncatedResponseError) -- distinct from
    # `rejected`: a rejection is the deterministic guard working correctly (good
    # news); a failure means QC never ran on that section at all (must be visible,
    # never swallowed -- this codebase's whole posture on LLM-call failures).
    failed: list[ProofreadFailure] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.applied


class Proofreader:
    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name

    # -- LLM boundary (override in tests) ------------------------------------
    def _complete(self, text: str) -> str:
        from creditpaper.llm.client import messages_create

        model = proofread_model(self._model_name)
        resp = messages_create(
            tool="agent:proofread",
            stage="proofread",
            model=model,
            max_tokens=18000,
            system=_INSTRUCTION,
            messages=[{"role": "user", "content": text}],
        )
        return resp.text

    def _corrections(self, text: str) -> list[dict]:
        objs = iter_json_objects(self._complete(text))
        for o in objs:
            if isinstance(o.get("corrections"), list):
                return o["corrections"]
        return []

    def proofread_block(self, section: int, block: ProseBlock, report: ProofreadReport) -> ProseBlock:
        """Apply language-only corrections to one section's prose, in place, logging
        each. A correction that would alter any figure or citation is refused."""
        text = block.text
        for c in self._corrections(text):
            before, after = str(c.get("before", "")), str(c.get("after", ""))
            if not before or before == after or before not in text:
                continue
            if _figures(before) != _figures(after):
                report.rejected.append(
                    f"§{section}: refused (would change a figure/citation): {before!r} -> {after!r}"
                )
                continue
            text = text.replace(before, after, 1)
            report.applied.append(Correction(section, before, after, str(c.get("kind", ""))))
        block.text = text
        return block

    def proofread(self, prose: dict[int, ProseBlock]) -> ProofreadReport:
        """Proofread every generated section, one LLM call per section, in parallel
        (matches the ThreadPoolExecutor(max_workers=6) pattern used for body-section
        generation in scripts/generate_paper.py). Mutates the blocks in place.

        Parallel is not just a latency win here: under staged delivery a section is
        shown to the analyst as soon as it's generated, so "delivered" must mean
        "final" -- that only holds if proofreading happens per-section rather than
        as an end-of-run sweep over the whole paper. Each worker gets its own
        ProofreadReport (no shared mutable report across threads) and results are
        merged back in section order, so run-to-run diffing -- the mechanism this
        project actually uses to validate changes -- stays meaningful regardless of
        which worker happens to finish first. A section whose call raises (e.g. a
        TruncatedResponseError) is dropped from the applied/rejected merge -- one
        bad section must never cost the rest of the paper its corrections -- but
        it is recorded in `report.failed`, not swallowed: a section that never got
        its QC pass must be visible, the same posture the truncation guard itself
        exists to enforce, and doubly so once staged delivery can hand an unproofed
        section to the analyst marked "done".
        """
        report = ProofreadReport()
        sections = sorted(prose)

        def _run(section: int) -> ProofreadReport:
            local = ProofreadReport()
            self.proofread_block(section, prose[section], local)
            return local

        results: dict[int, ProofreadReport] = {}
        failures: dict[int, BaseException] = {}
        with pool(6, thread_name_prefix="proofread") as ex:
            futs = {ex.submit(_run, section): section for section in sections}
            for fut in as_completed(futs):
                section = futs[fut]
                try:
                    results[section] = fut.result()
                except Exception as exc:  # noqa: BLE001 -- captured per-section, not swallowed
                    failures[section] = exc

        # Re-sorted into section order (and, within a section, proofread_block's own
        # sequential-append order) regardless of the completion order above.
        for section in sections:
            local = results.get(section)
            if local is not None:
                report.applied.extend(local.applied)
                report.rejected.extend(local.rejected)
            exc = failures.get(section)
            if exc is not None:
                report.failed.append(ProofreadFailure(section, type(exc).__name__, str(exc)))
        return report
