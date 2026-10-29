"""Verified facts onto the deal model, by concept, through the one write path.

Spec section 3 steps 7 and 8, D1 and D4 (Plan C Task 5).

``verify_and_build`` is the orchestration the pipeline and the scorecard share. For
each extracted fact it takes the fact's kind and materiality from its concept
(``model/concepts.py``), the deal currency from the caller (analyst-confirmed, DECISIONS
2026-09-19), passes the field path so the table layer can prove a cell's column, and
attaches the semantic support check to material facts only. The support check is off
unless a checker factory is passed.

``to_fact`` turns one verified fact into the ``Fact`` the model stores:

- status follows the verdict. VERIFIED is EXTRACTED; every other verdict (a flagged
  leg, ANCHORED, FLAGGED_ROLE, a refused quote) is FLAGGED. D4 holds twice: ``Fact``'s
  own validator refuses an anchorless extracted fact.
- confidence is the verification's, lowered by a second read that trusts it less.
  There is no constant confidence here and no threshold either: ``apply()`` routes
  low-confidence facts to the review queue on its own.
- provenance carries the page, the quote the extractor gave (``location``; the label
  store reads it from there), the registry version and the prompt version.
- ``basis`` is ``FactVerification.basis()`` and ``verification`` is its ``summary()``,
  so a later decision on the fact is labelled with the verdict the analyst was shown.
- it goes through ``concepts.qualified_fact``: the GST basis is required on money
  concepts, and a MIXED basis on material money flags the fact.

``commit`` writes through ``model/apply.py`` only (D1). Rejection is per field, never
per document: a path with no concept is refused as unmapped and routed to review (never
filed under a guessed field), and every other field of the document still lands.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from creditpaper import __version__
from creditpaper.extractors.base import Extractor
from creditpaper.ingest.dual_read import DualOutcome
from creditpaper.ingest.gaps import ReadCoverage, mark_unread_fields
from creditpaper.ingest.support import SupportChecker, checker_for
from creditpaper.ingest.verify import FactVerification, Verdict, verify_fact
from creditpaper.ingest.windows import Window
from creditpaper.model.apply import ApplyMode, ApplyResult, ExtractionOutput, apply
from creditpaper.model.concepts import REGISTRY_VERSION, Concept, concept_for, qualified_fact
from creditpaper.model.core import ExtractionMethod, Fact, FactStatus, Provenance
from creditpaper.model.deal import DealModel

# The currency a money figure must be stated in when the deal currency is not yet
# confirmed. The verifier's currency allowlist is AUD; an unconfirmed deal currency
# still makes a bare "$" CURRENCY_UNPROVEN (verify.py), so this proves nothing alone.
BASE_CURRENCY = "AUD"
UNMAPPED_NOTE = "unmapped: no concept for this path"
NOT_READ_NOTE = "not read"

# (path, concept) -> the support check bound to that field.
CheckerFactory = Callable[[str, Concept], SupportChecker]


@runtime_checkable
class SecondRead(Protocol):
    """What the dual read (Plan C Task 2) hands the commit: its confidence and the
    second model's value. Structural, so this module does not depend on Task 2."""

    confidence: float
    second_value: object


_DECIMAL_TAG = "__decimal__"


class VerifiedFact(BaseModel):
    """One extracted fact and its verification, ready to commit."""

    # `extra="forbid"` because the alternative is silent evidence loss. The two
    # ingestion lines named this field differently -- `version_id` against
    # `document_version_id` -- and pydantic's default is to IGNORE an unknown
    # keyword. A caller using the old name therefore built a fact whose
    # provenance named no version, which the ledger correctly refused to
    # release with "source version is missing, archived or superseded". The
    # fact was right; the message pointed at the document. Rejecting the
    # keyword names the real problem at the line that causes it.
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True, extra="forbid")

    path: str
    value: Any
    document_id: str
    document_version_id: str | None = None
    extractor_version: str
    model_id: str
    prompt_version: str | None = None
    # The quote the extractor gave, and the page it cited.
    #
    # Defaulted so the WINDOW pipeline can build one. That path never receives a
    # separate quote: the anchor already carries the matched text
    # (`verification.anchor.matched_text`), and under line anchoring the clause
    # is cut from the page rather than quoted by the model at all. Empty here
    # means "no model-supplied quote", not "no evidence".
    quote: str = ""
    cited_page: int | None = None
    method: ExtractionMethod = ExtractionMethod.LLM_EXTRACTION
    verification: FactVerification
    dual: SecondRead | None = None

    @field_serializer("value", when_used="json")
    def _tag_decimal(self, value: Any) -> Any:
        """Keep a Decimal a Decimal across a JSON round-trip.

        A held fact is stored as JSON and released later. Without this,
        `Decimal("12450000")` came back as a float or a string and every held
        numeric fact was unreleasable — the value that returned was not the
        value that was held.
        """
        return {_DECIMAL_TAG: str(value)} if isinstance(value, Decimal) else value

    @field_validator("value", mode="before")
    @classmethod
    def _untag_decimal(cls, value: Any) -> Any:
        if isinstance(value, dict) and set(value) == {_DECIMAL_TAG}:
            return Decimal(str(value[_DECIMAL_TAG]))
        return value


class UnmappedPath(ValueError):
    """A path no concept names: refused, routed to review, never guessed."""


def default_checker_factory(path: str, concept: Concept) -> SupportChecker:
    """The production support check for one field (``support.checker_for``). Spends."""
    return checker_for(path, concept.definition, kind=concept.kind)


def _figure_kinds(concept: Concept | None) -> tuple[str, ...] | None:
    """The figure kinds the value may be written in; None verifies it as untyped
    (text and bool are not figure kinds)."""
    if concept is None or concept.kind in ("text", "bool"):
        return None
    return concept.accepted_kinds


def verify_and_build(
    output: ExtractionOutput | Mapping[str, Fact[Any]],
    pages: Mapping[int, str],
    *,
    document_id: str,
    deal_currency: str | None,
    extractor_version: str,
    model_id: str,
    prompt_version: str | None = None,
    document_version_id: str | None = None,
    vision_pages: Collection[int] = frozenset(),
    checker_factory: CheckerFactory | None = None,
    duals: Mapping[str, SecondRead] | None = None,
) -> list[VerifiedFact]:
    """Verify each extracted fact of one document against its page texts.

    ``pages`` includes vision transcriptions; ``vision_pages`` names them, so a fact
    anchored there is capped for review. ``deal_currency`` is the analyst-confirmed
    currency, or None while unconfirmed. ``checker_factory`` (None: the support leg is
    off) is called for material facts only. ``duals`` maps a path to its second read.

    Facts without a value, and derived facts (computed after commit, anchored by a
    formula), are not extraction output and are not returned. An unmapped path is
    returned, verified as an untyped value, so ``commit`` can refuse and route it.
    """
    fields = output.fields if isinstance(output, ExtractionOutput) else output
    page_texts = dict(pages)
    built: list[VerifiedFact] = []
    for path, fact in fields.items():
        if fact.value is None:
            continue
        prov = fact.provenance
        if prov is not None and prov.method is ExtractionMethod.DERIVED:
            continue
        concept = concept_for(path)
        kinds = _figure_kinds(concept)
        material = concept is not None and concept.material
        dual = (duals or {}).get(path)
        kwargs: dict[str, Any] = {}
        if kinds is not None and "money" in kinds:
            kwargs["currency"] = deal_currency or BASE_CURRENCY
            kwargs["deal_currency"] = deal_currency
        if dual is not None:
            kwargs["second_read"] = dual.second_value
        checker = (
            checker_factory(path, concept)
            if checker_factory is not None and concept is not None and material
            else None
        )
        quote = prov.location if prov is not None else ""
        cited = prov.page if prov is not None else None
        verification = verify_fact(
            value=fact.value,
            quote=quote,
            pages=page_texts,
            kind=kinds,
            cited_page=cited,
            field=path,
            vision_pages=frozenset(vision_pages),
            material=material,
            support_checker=checker,
            **kwargs,
        )
        built.append(
            VerifiedFact(
                path=path,
                value=fact.value,
                document_id=document_id,
                document_version_id=(prov.document_version_id if prov else None) or document_version_id,
                extractor_version=(prov.extractor_version if prov else None) or extractor_version,
                model_id=(prov.model_id if prov else None) or model_id,
                prompt_version=(prov.prompt_version if prov else None) or prompt_version,
                quote=quote,
                cited_page=cited,
                method=prov.method if prov is not None else ExtractionMethod.LLM_EXTRACTION,
                verification=verification,
                dual=dual,
            )
        )
    return built


def to_fact(vf: VerifiedFact) -> Fact[Any]:
    """The fact as committed (module docstring). Raises ``UnmappedPath`` for a path no
    concept names, and ``ValueError`` when the concept's qualifiers cannot be met."""
    concept = concept_for(vf.path)
    if concept is None:
        raise UnmappedPath(f"{vf.path}: {UNMAPPED_NOTE} (registry {REGISTRY_VERSION})")
    v = vf.verification
    # The second read's OUTCOME, not just its number.
    #
    # `min(first, second)` was conservative but it threw away what the second
    # read is for: `llm/registry.py` refuses to let SECOND_READ share a
    # provider with EXTRACT, so agreement comes from a different model family
    # and is real evidence — taking the min discarded it. And a DISAGREEMENT
    # only lowered confidence; it has to FLAG, because two families reading one
    # figure differently is a person's decision, not a score.
    confidence = v.confidence
    dual_disagreed = False
    if vf.dual is not None:
        confidence = vf.dual.confidence
        dual_disagreed = getattr(vf.dual, "outcome", None) is DualOutcome.DISAGREED
    page = v.page if v.page is not None else vf.cited_page
    # The anchor, falling back to what verification actually LOCATED.
    #
    # `quote` is the model's own words and the window pipeline never has one:
    # under line anchoring the clause is cut from the page rather than quoted.
    # The ledger's `ck_assertion_anchor` requires a non-empty location — D4, no
    # anchor no field — so an empty quote made every window-pipeline fact
    # unreleasable, and the failure surfaced as a check violation rather than
    # as anything about anchors. The matched text is the better anchor anyway:
    # it is the page, not a reproduction of it.
    location = vf.quote or (v.anchor.matched_text if v.anchor else "") or (f"page {page}" if page else "")
    provenance = Provenance(
        document_id=vf.document_id,
        document_version_id=vf.document_version_id,
        page=page,
        location=location,
        method=vf.method,
        extractor_version=vf.extractor_version,
        model_id=vf.model_id,
        prompt_version=vf.prompt_version,
    )
    verified = v.verdict is Verdict.VERIFIED and v.page is not None and not dual_disagreed
    fact: Fact[Any] = Fact(
        value=vf.value,
        provenance=provenance,
        confidence=confidence,
        status=FactStatus.EXTRACTED if verified else FactStatus.FLAGGED,
        verification=v.summary(),
    )
    return qualified_fact(concept, fact, v.basis())


@dataclass
class Built:
    """``to_output`` plus the fields refused before the write path saw them."""

    output: ExtractionOutput
    rejected: dict[str, str] = field(default_factory=dict)
    unmapped: list[VerifiedFact] = field(default_factory=list)


def build(facts: Sequence[VerifiedFact], coverage: ReadCoverage | None = None) -> Built:
    fields: dict[str, Fact[Any]] = {}
    rejected: dict[str, str] = {}
    unmapped: list[VerifiedFact] = []
    flags: list[str] = []
    for vf in facts:
        try:
            fields[vf.path] = to_fact(vf)
        except UnmappedPath as exc:  # per field: the rest of the document still lands
            rejected[vf.path] = str(exc)
            unmapped.append(vf)
            flags.append(f"{UNMAPPED_NOTE}: {vf.path}")
        except ValueError as exc:
            rejected[vf.path] = str(exc)
    if coverage is not None and coverage.has_gaps:
        flags.append(f"{NOT_READ_NOTE} (pages {', '.join(str(p) for p in coverage.unread)})")
        # BOTH lines' behaviour, because each catches something the other does
        # not. The flag says the DOCUMENT has a gap; `mark_unread_fields` turns
        # a field anchored to an unread page into MISSING, which is the
        # stronger claim: a fact from a page we failed to read is not a fact we
        # hold, and leaving it EXTRACTED would cite evidence nobody looked at.
        fields = mark_unread_fields(fields, coverage)
    return Built(ExtractionOutput(fields=fields, flags=flags), rejected, unmapped)


def to_output(facts: Sequence[VerifiedFact], coverage: ReadCoverage | None = None) -> ExtractionOutput:
    """The committable output. Unmapped and unqualifiable fields are left out, and
    each unmapped path is named in a flag."""
    return build(facts, coverage).output


def commit(
    model: DealModel,
    facts: Sequence[VerifiedFact],
    coverage: ReadCoverage | None = None,
    *,
    tenant_id: str | None = None,
    deal_id: str | None = None,
) -> ApplyResult:
    """Write through ``model/apply.py``. Refusals before the write path join apply's
    own per-field ``rejected``. With a tenant and deal, an unmapped figure is routed to
    the review queue (spec step 10) and listed in ``routed_to_review``."""
    built = build(facts, coverage)
    result: ApplyResult = apply(model, built.output, ApplyMode.EXTRACT, tenant_id=tenant_id, deal_id=deal_id)
    result.rejected.update(built.rejected)
    if tenant_id is not None and deal_id is not None:
        from creditpaper.review.service import route_to_review

        for vf in built.unmapped:
            # No concept means no mapping to be confident in: 0.0 is the mapping's
            # confidence, the only way the queue takes an item whatever its verdict.
            queue_id = route_to_review(
                tenant_id=tenant_id,
                deal_id=deal_id,
                field_path=vf.path,
                document_id=vf.document_id,
                extracted_value=vf.value,
                confidence=0.0,
                page=vf.verification.page if vf.verification.page is not None else vf.cited_page,
            )
            if queue_id:
                result.routed_to_review.append(vf.path)
    return result


# ---------------------------------------------------------------------------
# THE WINDOW-JOB PIPELINE (from devel_jason, 2026-09-21)
#
# Two ingestion pipelines live in this module on purpose, pending a measured
# comparison (2026-09-23 decision). They are not alternatives yet:
#
#   verify_and_build / build / commit   concept-driven, from feat/ingestion:
#       a path maps to a Concept, the value is checked against that concept's
#       figure kinds, and a SupportChecker asks whether the span actually
#       SUPPORTS the claim.
#
#   extract_window / run_one_window     window-driven, from devel_jason:
#       an extractor is restricted to the pages one window covers and the
#       ledger runs one window per job, so a long document is split rather
#       than truncated.
#
# They share `VerifiedFact`, `to_fact` and `to_output`, which is why the merge
# had to union rather than choose. `ledger/jobs.py` calls `run_one_window` and
# `ingest/document.py` calls `extract_window`, so removing either breaks live
# wiring.
# ---------------------------------------------------------------------------


def _storage_path(tenant_id: str, version_id: str) -> Path:
    from creditpaper.db import tenant_cursor

    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT storage_path FROM deal_document_versions WHERE id = %s", (version_id,))
        row = cur.fetchone()
    if not row:
        raise LookupError(f"no version {version_id[:8]}")
    return Path(str(row[0]))


def extract_window(
    tenant_id: str,
    deal_id: str,
    payload: dict[str, Any],
    window: Window,
    *,
    registry: dict[str, type[Extractor]] | None = None,
) -> list[VerifiedFact]:
    """Run one window through its extractor(s) and keep every verified field.

    The verdict is NOT recomputed here. `extractors/_doc._scored` already
    reached one to set each fact's status, and the verification sink carries it
    across - two verifications of one fact are two things free to disagree.

    A group naming no registered extractor is skipped rather than fatal: the
    board deliberately shows more slots than the registry can read ("the board
    is not the extractor registry", Flow E), and that difference is the
    disclosure list, not an error.
    """
    from creditpaper.extractors._doc import page_text_context, verification_sink
    from creditpaper.harness.route import EXTRACTOR_REGISTRY
    from creditpaper.ingest import parse_cache
    from creditpaper.llm.registry import Task, resolve

    table: dict[str, type[Extractor]] = EXTRACTOR_REGISTRY if registry is None else registry
    content_hash = str(payload["content_hash"])
    version_id = str(payload["version_id"])
    document_id = str(payload.get("document_id") or version_id)

    parse = parse_cache.get(tenant_id, content_hash)
    if parse is None:
        # A cache miss is not an empty document. Returning no facts here would
        # read as "this document states nothing", which is a different finding
        # from "this document was never read".
        raise LookupError(f"no cached parse for {content_hash[:12]}")

    wanted = set(window.pages)
    pages = {p: t for p, t in parse.pages.items() if p in wanted}
    if not pages:
        return []

    page_texts = [{"page": p, "text": t} for p, t in sorted(pages.items())]
    path = _storage_path(tenant_id, version_id)
    # Spec §3.2: an unconfirmed machine reading is capped on this path too; it
    # was only ever capped on the corpus path and in the scorecard.
    from creditpaper.ingest.page_text import pages_for_version

    version_pages = pages_for_version(tenant_id, version_id)
    machine = version_pages.machine_read & wanted if version_pages is not None else frozenset()
    choice = resolve(Task.EXTRACT)
    model_id = f"{choice.provider}:{choice.model}"

    out: list[VerifiedFact] = []
    for slot in window.group.split("+"):
        extractor_cls = table.get(slot)
        if extractor_cls is None:
            continue
        extractor = extractor_cls()
        with page_text_context(pages, machine_read=frozenset(machine)), verification_sink() as sink:
            result = extractor.extract(document_id, path, page_texts=page_texts)
        for field_path, fact in result.fields.items():
            verification = sink.get(field_path)
            if verification is None:
                # Nothing was checked, so nothing is claimed. An unverifiable
                # field is not silently promoted to a verified fact.
                continue
            out.append(
                VerifiedFact(
                    path=field_path,
                    value=fact.value,
                    document_id=document_id,
                    document_version_id=version_id,
                    extractor_version=__version__,
                    model_id=model_id,
                    verification=verification,
                )
            )
    return out


def run_one_window(tenant_id: str, deal_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run one durable window job, retried and parked by the pipeline rules."""
    from creditpaper.ingest.pipeline_jobs import run_with_fallback

    window = Window(
        group=str(payload["group"]),
        pages=tuple(int(p) for p in payload["pages"]),
        reason="durable window",
    )
    outcome = run_with_fallback([("window", lambda: extract_window(tenant_id, deal_id, payload, window))])
    return {
        "status": "complete" if outcome.ok else "failed",
        "parked": outcome.parked,
        "attempts": [a.model_dump() for a in outcome.attempts],
    }
