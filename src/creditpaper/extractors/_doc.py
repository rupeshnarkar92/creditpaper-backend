"""Shared document-extraction plumbing for the LLM-backed extractors.

Two extraction paths are provided:

1. ``document_json_pages()`` — the preferred path (Pipeline Stage 3).
   Receives pre-rendered PNG pages from ``pages_to_png()`` (only the pages
   flagged by the page selector) plus their extracted text as context.
   NVIDIA uses anchored text alone; existing vision providers retain image blocks.

2. ``document_json()`` — legacy path, kept for backward compatibility and for
   documents where S2 page_texts are unavailable. Sends the full PDF as a
   base64 blob to the shared client. NVIDIA converts it locally to page text;
   Anthropic uses native PDF support and Ollama renders page images.

Every extracted value carries its page + source clause (the D4 anchor);
``add_fact`` enforces "no anchor, no field" and drops anything the model
can't locate to a clause.

The LLM call is isolated here so extractors stub ``_extract_raw`` in tests.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, field_validator

from creditpaper.agents.jsonio import extract_json_object
from creditpaper.ingest.anchor import MIN_QUOTE_WORDS
from creditpaper.llm.providers import LLMProvider
from creditpaper.model.citations import is_valid_path
from creditpaper.model.core import ExtractionMethod, Fact, FactStatus, Provenance
from creditpaper.obs.timing import timed

if TYPE_CHECKING:
    # Runtime import stays inside `_scored`: `ingest.verify` reaches the database
    # layer transitively, and importing it at module scope would make every
    # extractor import pull that in.
    from creditpaper.ingest.verify import FactVerification

_Extraction = TypeVar("_Extraction", bound=BaseModel)

# NO SAMPLING KNOBS. Extraction is not a creative task, and three runs of the
# same 144-document folder producing 128, 127 and 112 facts is a real cost. But
# the fix is not available: `temperature` was pinned to 0 here on 2026-09-21 and
# every extraction call then failed with
#
#     400 invalid_request_error: `temperature` is deprecated for this model.
#
# which `harness/determinism.py` had already recorded — Opus 5 and Sonnet 5
# accept no temperature, top_p or top_k. The unit test did not catch it because
# it mocks `messages_create` and asserts only that the kwarg was PASSED, never
# that the API accepts it.
#
# Run-to-run variance therefore cannot be removed at the sampler. It has to be
# removed downstream, by deriving values from the anchored span rather than
# trusting the value the model typed — see
# docs/specs/2026-09-22-deterministic-extraction.md.
#
# Do not reintroduce a sampling kwarg here without a LIVE call proving the
# model accepts it. `tests/ingest/test_extraction_is_deterministic.py` pins
# their absence.

# One definition, included by every extractor prompt. Nine prompts each wrote
# their own version of this and all nine asked for a "short verbatim clause" —
# which is precisely what `anchor.py` refuses. Measured on a real 144-document
# folder: 110 of 111 clause examples were under the six-word minimum, 79 of them
# a single word, and 0 of 40 value+clause pairs passed their own verifier. The
# model copied the format we demonstrated and we rejected every fact it made.
#
# The last paragraph is deliberate. The first version told the model to OMIT any
# field it could not anchor with a full sentence, and 15 facts disappeared
# between runs. A DROPPED fact is invisible; a FLAGGED fact is a disclosure in
# the review queue that a person can act on. D4 still holds - a value with no
# page and no clause at all is not a fact - but "the best quote available" beats
# silence.
ANCHOR_RULE = (
    "ANCHOR RULE — applied to EVERY value you return.\n"
    f"`clause` MUST be at least {MIN_QUOTE_WORDS} words. A shorter quote cannot "
    "be located on the page, so the fact is DISCARDED — not flagged, not "
    "reviewed, discarded. This is the most common way a CORRECT fact is lost.\n"
    "Quote the COMPLETE SENTENCE, or the WHOLE TABLE ROW, or the whole form "
    "line that CONTAINS the value — verbatim, including the value itself "
    "exactly as it appears on the page.\n"
    'A label on its own — "Borrower", "Note 15", "Contract Sum" — is never '
    "enough. For a figure in a table, quote the ENTIRE ROW: its label and every "
    "figure on it.\n"
    f"If the sentence holding the value is under {MIN_QUOTE_WORDS} words, KEEP "
    "READING — extend the quote into the text immediately before or after it "
    "until you have at least that many. Contiguous text only: never join parts "
    "of the page that are not next to each other.\n"
    "Omit the field only when you have neither a page nor any quote at all."
)


@timed("validation")
def validate_extraction(schema: type[_Extraction], value: dict) -> _Extraction:
    """Time existing typed validation without logging input or validation errors."""
    return schema.model_validate(value)


class Anchored(BaseModel):
    """A value plus its parse-time anchor (D4): the page and a verbatim clause."""

    value: str | int | float | None = None
    page: int | None = None
    clause: str = ""

    @field_validator("clause", mode="before")
    @classmethod
    def _none_clause_to_empty(cls, v: object) -> str:
        # Models sometimes emit an explicit "clause": null rather than omitting it;
        # treat that as no clause, not a validation failure that drops the whole
        # extraction (the Wattlefield blind run failed the entire valuation this way).
        return "" if v is None else v


def b64(path: str | Path) -> str:
    return base64.standard_b64encode(Path(path).read_bytes()).decode("utf-8")


def pages_to_png(
    path: str | Path,
    page_indices: Sequence[int],
    *,
    dpi: int | None = None,
) -> list[str]:
    """Render selected PDF pages to base64-encoded PNG strings via pypdfium2.

    Args:
        path: Path to the PDF file.
        page_indices: 0-based page indices to render (from page_selector).
        dpi: Render resolution. None = auto (72 for Ollama, 150 for Anthropic).
             Raise to 200 for dense tables or small-print reports.

    Returns:
        List of base64-encoded PNG strings, one per requested page, in order.
        Pages that cannot be rendered (e.g. index out of range) are skipped.
    """
    from creditpaper.llm.registry import Capability, Task, resolve, supports

    choice = resolve(Task.EXTRACT)
    provider = LLMProvider(choice.provider)
    if not supports(choice, Capability.IMAGES):
        return []  # Text-only extraction consumes the accompanying anchored text.
    if dpi is None:
        dpi = 72 if provider == LLMProvider.OLLAMA else 150
    import io

    from creditpaper.ingest.reader import render_pages

    result: list[str] = []
    for image in render_pages(Path(path), list(page_indices), dpi=dpi):
        if image is None:
            continue
        out = io.BytesIO()
        image.save(out, format="PNG")
        result.append(base64.standard_b64encode(out.getvalue()).decode("utf-8"))
    return result


def document_json_pages(
    png_pages_b64: list[str] | None,
    instruction: str,
    text_context: str = "",
    model_name: str | None = None,
    *,
    max_tokens: int = 8000,
    tool: str = "extract",
    client=None,
) -> dict:
    """Send pre-rendered PNG pages + text context to the vision LLM.

    This is the **preferred extraction path** (Stage 3 of the pipeline).
    Only the pages flagged by the page selector are sent, not the full PDF.

    Args:
        png_pages_b64: Base64-encoded PNG strings from ``pages_to_png()``.
        instruction: The extraction prompt / schema instruction.
        text_context: Optional text extracted in S2 (provides textual grounding
                      alongside the visual representation — helps the model
                      locate exact clause wording for D4 anchors).
        model_name: Override for ANTHROPIC_EXTRACT_MODEL.
        max_tokens: Max completion tokens.
        tool: Stage identifier for LLM client routing.
        client: Test seam (mirrors messages_create's parameter).

    Returns:
        Parsed JSON dict of extracted facts.
    """
    from creditpaper.llm.client import messages_create
    from creditpaper.llm.registry import Task, resolve

    choice = resolve(Task.EXTRACT)
    model = model_name or choice.model
    content: list[dict] = []

    # PNG image blocks — one per selected page
    for png_b64 in png_pages_b64 or []:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": png_b64,
                },
            }
        )

    # Optional text context block (extracted text from S2)
    if text_context:
        # Ollama: shorter context to fit within tighter token limits.
        # The text is what the model copies its verbatim anchors from, and pages are in
        # order: a 6000-char cap dropped everything after page ~2 of a 10-page pass
        # (the ITS funding table sits at the back). 10 pages of text is ~10k tokens.
        provider = LLMProvider(choice.provider)
        max_ctx = (
            int(os.getenv("OLLAMA_TEXT_CONTEXT_CHARS", "48000")) if provider == LLMProvider.OLLAMA else 60000
        )
        context = text_context if provider == LLMProvider.NVIDIA else text_context[:max_ctx]
        content.append(
            {
                "type": "text",
                "text": f"Extracted text from these pages (for clause location):\n\n{context}",
            }
        )

    # Instruction / schema
    content.append({"type": "text", "text": instruction})

    resp = messages_create(
        client=client,
        tool=tool,
        stage="extract",
        provider=LLMProvider(choice.provider),
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": content}],
    )
    return extract_json_object(resp.text)


def document_json(
    pdf_b64: str,
    instruction: str,
    model_name: str | None = None,
    *,
    cache_doc: bool = True,
    max_tokens: int = 8000,
    tool: str = "extract",
    client=None,
) -> dict:
    """Build and make one document-extraction call, returning the parsed object.

    `client` is a test seam, mirroring `messages_create`'s. This function decides
    three things that are invisible in the output if they are wrong — the
    `cache_control` marker (without it the valuation extractor's three passes
    each re-send the full PDF at full price), `stage="extract"` (what the effort
    dial keys on), and block order — so being able to inspect the assembled
    request without a network call is what makes any of that testable.
    """
    from creditpaper.llm.client import messages_create
    from creditpaper.llm.registry import Task, resolve

    choice = resolve(Task.EXTRACT)
    model = model_name or choice.model
    doc_block: dict = {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64},
    }
    if cache_doc:
        # Cache the document so multiple topic-targeted passes over the same report
        # (values, comps, market/site) read it cheaply instead of re-sending it.
        doc_block["cache_control"] = {"type": "ephemeral"}
    resp = messages_create(
        client=client,
        # stage="extract", not `tool`: every extractor's `tool` differs
        # (extract:its, extract:valuation/comps, ...) but they all share one
        # ANTHROPIC_EFFORT_EXTRACT dial -- extraction is JSON-shape work, tuned
        # as one stage, not per document type.
        tool=tool,
        stage="extract",
        provider=LLMProvider(choice.provider),
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": [doc_block, {"type": "text", "text": instruction}]}],
    )
    text = resp.text
    return extract_json_object(text)


# --- Derived confidence -------------------------------------------------------
# Every fact here carried confidence=0.9 from the extraction spine (318c2d3,
# 2026-06-17) onward. Nothing could produce a real number then: the extractor
# knows only that the model returned a value and that it had an anchor, and a
# model's own confidence estimate is uncalibrated. The cost was that the review
# threshold (0.7) sat below the constant, so the confidence route to human
# review never fired once - a quote present verbatim and a quote appearing
# nowhere both scored 0.9.
#
# The page text is carried in a ContextVar rather than through `add_fact`'s 23
# call sites. Extraction runs six-wide through `concurrency.pool`, which copies
# the submitting thread's context per task, so each document's pages stay its
# own - the same mechanism the cost ledger already relies on.
_PAGE_TEXT: ContextVar[dict[int, str] | None] = ContextVar("extraction_page_text", default=None)
# Pages whose text is an unconfirmed machine reading (OCR or vision): a fact
# anchored there is capped for review (`ingest/verify.py`, VISION_TEXT).
_MACHINE_READ: ContextVar[frozenset[int]] = ContextVar("extraction_machine_read", default=frozenset())


@contextmanager
def page_text_context(
    pages: dict[int, str] | None, *, machine_read: frozenset[int] = frozenset()
) -> Iterator[None]:
    """Make a document's page text, and which pages are machine-read, available to the fact builders below."""
    token = _PAGE_TEXT.set(pages or None)
    machine_token = _MACHINE_READ.set(machine_read)
    try:
        yield
    finally:
        _MACHINE_READ.reset(machine_token)
        _PAGE_TEXT.reset(token)


_VERIFICATIONS: ContextVar[dict[str, FactVerification] | None] = ContextVar(
    "extraction_verifications", default=None
)


@contextmanager
def verification_sink() -> Iterator[dict[str, FactVerification]]:
    """Collect each fact's verification, keyed by its model path.

    Opt-in: extraction outside this context behaves exactly as before. The
    window pipeline opens one so it can build VerifiedFacts from the verdict
    that already set each fact's status — verifying a second time downstream
    would be two judgements about one fact, free to disagree.

    Only checkable facts are recorded. A quote too short to verify leaves no
    entry, so a caller cannot read silence as a passing verdict.
    """
    sink: dict[str, FactVerification] = {}
    token = _VERIFICATIONS.set(sink)
    try:
        yield sink
    finally:
        _VERIFICATIONS.reset(token)


def pages_from_s2(page_texts: list[dict[str, object]] | None) -> dict[int, str]:
    """S2's `[{page, text}]` as the {page: text} the verifier reads."""
    if not page_texts:
        return {}
    out: dict[int, str] = {}
    for entry in page_texts:
        try:
            number = int(entry.get("page", 0) or 0)
        except (TypeError, ValueError):
            continue
        if number > 0:
            out[number] = str(entry.get("text") or "")
    return out


def _scored(
    value: object, anc: Anchored, path: str | None = None, *, check_value: bool = True
) -> tuple[float | None, bool]:
    """(confidence, flagged) for one anchored value, from what could be checked.

    Three outcomes, kept apart on purpose:
      * checked and sound -> a derived confidence above the review threshold;
      * checked and failed -> a derived confidence below it, and flagged;
      * not checkable -> None, which the ledger already treats as unscored and
        routes to review. Unverifiable is not the same as wrong, and it is
        certainly not 0.9.

    `path` is the fact's model path. When a `verification_sink` is open it is
    the key the whole verdict is filed under, so the window pipeline can build a
    VerifiedFact from the judgement made here rather than making its own.
    """
    from creditpaper.ingest.verify import Verdict, verify_fact

    pages = _PAGE_TEXT.get()
    if not pages or not anc.clause:
        return None, False

    verification = verify_fact(
        value=value,
        quote=anc.clause,
        pages=pages,
        cited_page=anc.page,
        check_value=check_value,
        vision_pages=_MACHINE_READ.get(),
    )
    if verification.verdict is Verdict.REFUSED_SHORT_QUOTE:
        # Three-word quotes false-match 10% of the time; this one cannot be
        # checked either way, so nothing is recorded for it.
        return None, False

    sink = _VERIFICATIONS.get()
    if sink is not None and path is not None:
        sink[path] = verification

    if verification.verdict is Verdict.VERIFIED:
        return verification.confidence, False
    return verification.confidence, True


def add_fact(
    fields: dict[str, Fact],
    flags: list[str],
    document_id: str,
    path: str,
    anc: Anchored | None,
    *,
    as_int: bool = False,
    check_value: bool = True,
) -> None:
    """Add one anchored fact to the extraction output, or drop+flag it. Enforces
    D4 (no clause and no page → not a fact) and the citation-path grammar.

    Every drop is now flagged (this branch used to return silently, which is how a
    field can vanish between two runs of the same extractor over the same document
    without a trace — see the determinism harness, harness/determinism.py). Wording
    keeps two DIFFERENT failure modes apart: the model never returning a value for
    the field at all ("no value returned by the model" — an omission, and a
    candidate explanation for run-to-run flapping) versus the model returning a
    value it could not anchor to a page or clause ("no anchor (D4)" — a provenance
    failure). Collapsing both into one message would erase that distinction for
    whoever reads the flag."""
    if anc is None or anc.value is None or str(anc.value).strip() == "":
        flags.append(f"{path}: dropped — no value returned by the model")
        return
    if not anc.clause and anc.page is None:
        flags.append(f"{path}: dropped — no anchor (D4)")
        return
    if not is_valid_path(path):
        flags.append(f"{path}: invalid path")
        return
    raw = str(anc.value).replace(",", "").replace("$", "").replace("%", "").strip()
    try:
        value: object = int(raw) if as_int else Decimal(raw)
    except (InvalidOperation, ValueError):
        value = str(anc.value)  # string fact (entity name, address, ...)
    confidence, flagged = _scored(value, anc, path, check_value=check_value)
    fields[path] = Fact(
        value=value,
        status=FactStatus.FLAGGED if flagged else FactStatus.EXTRACTED,
        confidence=confidence,
        provenance=Provenance(
            document_id=document_id,
            page=anc.page,
            location=anc.clause or f"page {anc.page}",
            method=ExtractionMethod.LLM_EXTRACTION,
        ),
    )


# Boolean resolution is TRI-STATE, not truthy/falsy. Anything outside both sets is
# UNKNOWN and is dropped with a flag, never defaulted.
#
# This was a whitelist of seven affirmative words with everything else falling to
# False. `screening.aml.sanctions` is True when A HIT WAS FOUND (see
# `screening.py`), so a screen worded "Match found" recorded a sanctions hit as a
# clean screen — silently, in a paper a credit committee signs. "Status: Current"
# did the same to a builder's licence.
#
# Keep both sets to wordings that are unambiguous as an affirmation or a negation
# of the field's own question. When in doubt leave a wording out: a dropped fact is
# visible to the analyst, a wrongly-resolved one is not.
_TRUE_WORDS = {
    "true",
    "yes",
    "y",
    "1",
    "current",
    "in force",
    "in-force",
    "valid",
    "active",
    "commenced",
    "appropriate",
    "reasonable",
    "satisfied",
    "confirmed",
    "verified",
    # screening verdicts: an affirmative answer to "was something found?"
    "match",
    "match found",
    "hit",
    "positive",
    "adverse",
    "found",
}
_FALSE_WORDS = {
    "false",
    "no",
    "n",
    "0",
    "none",
    "nil",
    "not current",
    "expired",
    "lapsed",
    "not commenced",
    "inactive",
    # screening verdicts: a negative answer to "was something found?"
    "clear",
    "cleared",
    "no match",
    "no matches",
    "negative",
    "not found",
    "none found",
}


def add_note_fact(
    fields: dict[str, Fact],
    flags: list[str],
    document_id: str,
    path: str,
    anc: Anchored | None,
) -> None:
    """A fact whose value is a JUDGEMENT or SUMMARY, verified on its anchor alone.

    `value_in_span` asks whether the value appears literally inside its own
    quote. For "revenue up 12% on prior year", or "no adverse judgments
    recorded", or a market-conditions paragraph, the value is a paraphrase of
    the clause - which is what the prompt asked the model to produce. It can
    never be a substring of the thing it summarises.

    Measured 2026-09-21: 26% of facts on a real folder came back FLAGGED_VALUE,
    and the flagged paths were overwhelmingly these fields. We were flagging
    them for being exactly what we requested.

    So the anchor is the whole of the machine check: the quote must exist, on
    the page it cites. Whether the quote SUPPORTS the summary is a judgement,
    and judgement is what the review queue is for.
    """
    add_fact(fields, flags, document_id, path, anc, check_value=False)


def add_bool_fact(
    fields: dict[str, Fact], flags: list[str], document_id: str, path: str, anc: Anchored | None
) -> None:
    """Emit a typed bool Fact with its anchor. Model fields like `da.approval_current`
    and `qs.agreement_appropriate` are Fact[bool]; a bare string would not coerce on
    assignment (cf. deal.type), so the boolean is resolved here.

    Mirrors `add_fact`'s contract deliberately: every drop is flagged, and the path
    is validated before anything is written. The two diverged when `add_fact` was
    made loud (fdda2d8) and its sibling was missed.
    """
    if anc is None or anc.value is None or str(anc.value).strip() == "":
        flags.append(f"{path}: dropped — no value returned by the model")
        return
    if not anc.clause and anc.page is None:
        flags.append(f"{path}: dropped — no anchor (D4)")
        return
    if not is_valid_path(path):
        flags.append(f"{path}: invalid path")
        return
    word = str(anc.value).strip().lower()
    if word in _TRUE_WORDS:
        truthy = True
    elif word in _FALSE_WORDS:
        truthy = False
    else:
        flags.append(f"{path}: dropped — boolean not recognised, analyst to confirm ({anc.value!r})")
        return
    # A boolean is this extractor's READING of the clause; the word "true" is
    # nowhere on the page, so the value leg cannot apply. The anchor is the check.
    confidence, flagged = _scored(anc.value, anc, path, check_value=False)
    fields[path] = Fact(
        value=truthy,
        status=FactStatus.FLAGGED if flagged else FactStatus.EXTRACTED,
        confidence=confidence,
        provenance=Provenance(
            document_id=document_id,
            page=anc.page,
            location=anc.clause or f"page {anc.page}",
            method=ExtractionMethod.LLM_EXTRACTION,
        ),
    )
