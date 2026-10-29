"""Ingestion pipeline — documents → typed model, with reconciliation.

Routes a deal's documents to extractors, runs them IN PARALLEL (each reads its own
PDF; preserves the quick-to-generate principle — wall-clock ≈ slowest doc, not the
sum), applies the results through the single write path, then RECONCILES: the
derivation engine's funding identity plus named cross-document checks surface
extraction errors as inconsistencies (the "catch"). Unreconciled items are held for
the human (>98% automated, the residual confirmed), not shipped silently.

Generation runs OFF the resulting model, decoupled from ingestion — so the paper
still generates fast and regenerates instantly.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import as_completed
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from creditpaper.concurrency import pool
from creditpaper.harness.route import EXTRACTOR_REGISTRY
from creditpaper.model import (
    DealModel,
    DealType,
    apply,
    deal_type_str,
    derive_delivery_model,
    derive_economics,
    derive_metrics,
)
from creditpaper.model.apply import ApplyMode, ExtractionOutput
from creditpaper.model.core import Fact
from creditpaper.obs.ledger import cost_session
from creditpaper.obs.timing import timed


@dataclass
class IngestReport:
    outputs: list[ExtractionOutput] = field(default_factory=list, repr=False)
    failed_documents: int = 0
    facts_by_slot: dict[str, int] = field(default_factory=dict)
    applied: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)  # a second document agreed
    extractor_flags: list[str] = field(default_factory=list)  # D4 drops, invalid paths
    reconciliation: list[str] = field(default_factory=list)  # cross-doc inconsistencies → HITL
    cost_usd: float = 0.0  # ingestion's slice of cost-of-sale
    wall_clock_s: float = 0.0  # elapsed (parallel) — what the user waits
    cost_summary: str = ""  # per-tool breakdown (CostLedger.summary())
    # Material concepts every window was asked about and none stated (spec §3.3).
    # Read by the readiness tiers: "asked and not stated" is not "never asked".
    material_not_stated: list[str] = field(default_factory=list)

    @property
    def reconciled(self) -> bool:
        return not self.reconciliation


@timed("ingest")
def ingest(
    model: DealModel,
    mappings: dict[str, Path | list[Path]],
    *,
    page_texts_map: dict[Path, list[dict]] | None = None,
    registry: dict | None = None,
    max_workers: int = 6,
    document_ids: dict[Path, tuple[str, str]] | None = None,
) -> IngestReport:
    """Extract the routed documents in parallel, apply, derive, and reconcile.

    `mappings` values may be a single Path or a list of Paths (multi-doc slots —
    insurance certs by cover, VEDA + AML screening); each doc becomes its own parallel
    extraction and the apply layer merges the results by key.

    Cost + latency are captured on the report (the cost-of-sale instrument): the whole
    run is scoped in a `cost_session`, and because extraction is parallel the recorded
    wall-clock is what the user waits, not the sum of per-doc latencies."""
    registry = registry or EXTRACTOR_REGISTRY
    rep = IngestReport()
    todo = [
        (slot, Path(p))
        for slot, val in mappings.items()
        if slot in registry
        for p in (val if isinstance(val, (list, tuple)) else [val])
    ]

    with cost_session(deal_id=model.deal_id) as ledger:
        _run_ingest(model, todo, registry, max_workers, rep, page_texts_map, document_ids)
    rep.cost_usd = ledger.total_usd
    rep.wall_clock_s = ledger.wall_clock_s
    rep.cost_summary = ledger.summary()
    return rep


# Read each window THREE times and union the draws.
#
# Measured 2026-09-24, blind_03, 300 windows, 3 independent runs, same sample
# dumped and re-cut both ways. The all-paths stability number barely moves
# (28.0% -> 30.3%) because collection rows dominate it and a wider read finds
# more of them. Underneath, where a credit paper is actually built:
#
# Both cuts re-measured under concept registry v3 with
# `scripts/stability_cut.py`, from the saved dumps, so the two columns differ
# only in the pass count:
#
#                        passes=1   passes=3
#   plain scalars           25.0%      45.9%
#   material concepts       18.8%      47.1%
#   material AND plain      18.2%      77.8%
#
# The number that decided it: material scalar facts that GO MISSING between
# reads, 3 of 11 at one pass and 0 of 9 at three. A fact sheet may not lose a
# fact when the same documents are read again. `valuation.valuer`,
# `property.address` and `title_reference` were each value-divergent at one
# pass and are stable at three - consensus resolves a document's two renderings
# of one fact by majority. What remains divergent (`da.reference`,
# `da.approved_date`) is a real ambiguity, an original consent against its
# s4.55 modification, and `consensus.merge` marks it contested so it is FLAGGED
# for review rather than silently decided by which draw landed first.
#
# It costs 3x: ~$15.50 for a full deal against ~$5.30. Against three deals for
# $499 that is ~9% COGS, and the thing being bought is that a fact sheet does
# not change under the analyst between two reads of the same documents.
#
# Superseded 2026-10-09 (DECISIONS): at temperature 0 one read is repeatable
# (measured 92% of paths and 100% of material scalars stable), and the targeted
# list, field-set and vision passes carry completeness. One read, temperature 0.
DEFAULT_PASSES = 1
DEFAULT_TEMPERATURE = 0.0


def _confirm_by_vision(
    outputs: list[ExtractionOutput],
    document_ids: Mapping[Path, tuple[str, str]],
    rep: IngestReport,
    *,
    model_id: str,
    provider: Any,
) -> None:
    """Second reads of scanned pages, cheapest first (`vision_check.confirm_in_tiers`).

    1. A second LOCAL OCR engine (`processing/ocr_rapid.py`) reads the scanned page of
       every fact capped for OCR. Free, so it covers every such fact, not only material
       ones. Where it states the same value the cap is lifted (`ocr_confirmed`).
    2. Claude vision reads only the scanned pages a MATERIAL or LIST fact stands on that
       step 1 disputed or could not read, within a page budget. It sends page images, so
       it runs only where the call reaches Anthropic."""
    from creditpaper.ingest import list_pass, vision_check
    from creditpaper.llm.client import effective_provider
    from creditpaper.llm.providers import LLMProvider
    from creditpaper.model.concepts import concept_for

    collections = tuple(c + "[" for spec in list_pass.LISTS.values() for c in spec.slots.values())

    def wanted(path: str) -> bool:
        concept = concept_for(path)
        return bool(concept is not None and concept.material) or path.startswith(collections)

    files = {doc: path for path, (doc, _version) in document_ids.items()}
    paid = effective_provider(provider) == LLMProvider.ANTHROPIC
    tiers = [
        vision_check.Tier(
            "ocr",
            vision_check.read_page_by_second_ocr(),
            wanted=lambda path: True,
            max_pages=OCR_CHECK_PAGES,
            workers=os.cpu_count() or 4,
            same_line=True,
        ),
        vision_check.Tier(
            "vision", vision_check.read_page_by_vision(model=model_id) if paid else None, wanted=wanted
        ),
    ]
    for tier, result in zip(tiers, vision_check.confirm_in_tiers(outputs, files, tiers), strict=True):
        if result.candidates:
            rep.extractor_flags.append(
                f"{tier.name} check: {result.pages_read} scanned pages read for {result.candidates} facts; "
                f"{result.confirmed} confirmed, {result.disputed} disputed"
                + (f", {result.failed} unreadable" if result.failed else "")
                + (f", {result.skipped_over_budget} pages over budget" if result.skipped_over_budget else "")
            )


# Scanned pages the free local second read covers per deal. Its cost is CPU only
# (~1-2 s a page on one core, read in parallel), so the budget is a ceiling against
# a pathological deal, not a spend control.
OCR_CHECK_PAGES = 300


# Pages per list-pass window: a list (a comparable schedule, a funding table) runs
# across pages, and these windows are few, so they are wider than the harvest's.
LIST_WINDOW = 4
LIST_PASSES = 2


def _run_list_passes(
    pages: list[Any],
    slot_of: Mapping[str, str],
    rep: IngestReport,
    *,
    tenant_id: str,
    deal_id: str,
    model_id: str,
    provider: Any,
    reference: str,
    checker_factory: Any,
) -> tuple[list[ExtractionOutput], set[str]] | None:
    """Every list pass over the documents filed in its slots: the outputs and the collections they cover."""
    from creditpaper.concurrency import pool
    from creditpaper.ingest import list_pass
    from creditpaper.ingest.general import extract_corpus

    jobs: list[tuple[Any, str, list[Any]]] = []
    for spec in list_pass.LISTS.values():
        for slot in spec.slots:
            chosen = list_pass.select_pages(spec, pages, {d: s for d, s in slot_of.items() if s == slot})
            if chosen:
                jobs.append((spec, slot, chosen))
    # Field sets: one document type's figures, each asked for by name (the valuer's values).
    for field_set in list_pass.FIELD_SETS.values():
        chosen = list_pass.select_pages(field_set, pages, slot_of)  # type: ignore[arg-type]
        if chosen:
            jobs.append((field_set, "", chosen))
    if not jobs:
        return None

    def run(job: tuple[Any, str, list[Any]]) -> tuple[list[ExtractionOutput], Any]:
        spec, slot, chosen = job
        # use_cache=False: a list window can share its content hash with a harvest
        # window, and the cache would replay the harvest's answer as this list's.
        # Two reads: one read's list of items varies (measured 17 against 24
        # comparables on two reads of blind_01); the union, one row per item, does not.
        outs, report = extract_corpus(
            chosen,
            list_pass.list_harvester(spec, slot, model_id=model_id, provider=provider)
            if isinstance(spec, list_pass.ListSpec)
            else list_pass.field_set_harvester(spec, model_id=model_id, provider=provider),
            tenant_id=tenant_id,
            deal_id=deal_id,
            model_id=model_id,
            passes=LIST_PASSES,
            window=LIST_WINDOW,
            use_cache=False,
            reference=reference,
            checker_factory=checker_factory,
        )
        if isinstance(spec, list_pass.ListSpec):
            outs = list_pass.dedupe(outs, spec, spec.collection_for(slot))
        return outs, report

    with pool(min(len(jobs), 8), thread_name_prefix="list-pass") as executor:
        results = list(executor.map(run, jobs))
    listed: list[ExtractionOutput] = [o for outs, _ in results for o in outs]
    collections = {
        c for spec, _, _ in jobs if isinstance(spec, list_pass.ListSpec) for c in spec.slots.values()
    }
    facts = sum(len(o.fields) for o in listed)
    unread = sum(r.windows_unread for _, r in results)
    rep.extractor_flags.append(
        f"lists: {len(jobs)} list passes, {sum(r.windows for _, r in results)} windows, {facts} facts"
        + (f", {unread} windows unread" if unread else "")
    )
    return listed, collections


@timed("ingest_corpus")
def ingest_corpus(
    model: DealModel,
    page_texts_map: dict[Path, list[dict]],
    *,
    document_ids: dict[Path, tuple[str, str]],
    tenant_id: str,
    deal_id: str,
    passes: int = DEFAULT_PASSES,
    window: int | None = None,
    use_cache: bool = True,
    primary_blocks: dict[str, set[str]] | None = None,
    support: bool = True,
    material: bool = True,
    temperature: float | None = DEFAULT_TEMPERATURE,
    slot_of: Mapping[str, str] | None = None,
    lists: bool = True,
    vision: bool = True,
) -> IngestReport:
    """Read the WHOLE corpus once, apply, derive, and reconcile.

    The sibling of `ingest()`. That one routes each confirmed document to the
    extractor registered for its slot; this one reads every page of every
    document against the generated field reference, so a document whose slot
    has no registered extractor is still read. The board carries far more slot
    types than the registry can read, and today those documents contribute
    nothing at all.

    It takes no `mappings`, because it needs none: there is no per-slot
    extractor to choose. D3 is still honoured upstream — the caller passes only
    documents the analyst has placed in a decided slot.

    Both paths converge on `apply_outputs`, so what happens to a fact after it
    is extracted cannot fork between them.
    """
    from creditpaper.ingest import material_pass
    from creditpaper.ingest.commit import default_checker_factory
    from creditpaper.ingest.general import extract_corpus, pages_from_map
    from creditpaper.ingest.harvest import build_harvester
    from creditpaper.llm.registry import Task, resolve
    from creditpaper.paths import field_reference

    rep = IngestReport()
    pages = pages_from_map(page_texts_map, document_ids)
    if not pages:
        return rep

    # The version for each document, so `stamp_provenance` can resolve a fact
    # to its stored version by the document it names.
    versions: dict[str, tuple[str, str]] = {doc: (doc, ver) for doc, ver in document_ids.values()}

    # The registry resolves both the task model and provider. An active tenant
    # BYOK scope remains authoritative inside `messages_create`.
    # The provider travels separately; `model_id` is what the provider's API is
    # sent, so it is the bare model name. "anthropic:claude-..." is not a model
    # any provider knows, and without BYOK nothing rewrote it before the call.
    choice = resolve(Task.EXTRACT)
    model_id = choice.model
    reference = field_reference().read_text(encoding="utf-8")
    # Temperature 0 makes a read repeatable; a model that rejects it is not sent one (llm/client.py).
    harvester = build_harvester(
        model_id=model_id, provider=choice.provider, reference=reference, temperature=temperature
    )
    # One fixed question per material concept, per window (spec §3.3): the open
    # harvest lets the model choose what to mention, so a material scalar can go
    # missing between reads; this pass asks for each one by name.
    material_reader = (
        material_pass.build_material_reader(
            model_id=model_id, provider=choice.provider, temperature=temperature
        )
        if material
        else None
    )

    # The semantic leg. `value_in_page_span` proves a value is stated inside
    # its span; it cannot tell that from the span stating it OF THIS FIELD,
    # which is the error general extraction uniquely creates. `support.py`
    # asks exactly that question, and `commit.py` already uses it on the
    # window path.
    #
    # It spends a model call per fact, so `primary_blocks` triages: a fact
    # landing outside the blocks its own document primarily feeds is where the
    # risk concentrates. On blind_01 that is 196 facts of 517. The concept
    # registry's own `material` flag is the floor beneath this and applies
    # regardless.
    checker_factory = default_checker_factory if support else None
    material_for = _outside_primary_blocks(primary_blocks) if primary_blocks else None

    with cost_session(deal_id=model.deal_id) as ledger:
        from creditpaper.concurrency import pool

        def general() -> tuple[list[ExtractionOutput], Any]:
            return extract_corpus(
                pages,
                harvester,
                tenant_id=tenant_id,
                deal_id=deal_id,
                model_id=model_id,
                passes=passes,
                use_cache=use_cache,
                reference=reference,
                checker_factory=checker_factory,
                material_for=material_for,
                material_reader=material_reader,
                **({"window": window} if window is not None else {}),
            )

        def listed() -> tuple[list[ExtractionOutput], set[str]] | None:
            # Targeted list passes (ingest/list_pass.py): every comparable, presale,
            # funding line and covenant, from the pages of the documents filed in each
            # list's slots. They need each document's slot; without it they do not run.
            if not (lists and slot_of):
                return None
            return _run_list_passes(
                pages,
                slot_of,
                rep,
                tenant_id=tenant_id,
                deal_id=deal_id,
                model_id=model_id,
                provider=choice.provider,
                reference=reference,
                checker_factory=checker_factory,
            )

        # Independent reads of the same pages: run side by side, not one after the other.
        with pool(2, thread_name_prefix="corpus") as executor:
            general_future, list_future = executor.submit(general), executor.submit(listed)
            outputs, general_report = general_future.result()
            list_result = list_future.result()
        if list_result is not None:
            from creditpaper.ingest import list_pass

            listed_outputs, collections = list_result
            # Each list pass takes over its collection for the documents it covers.
            outputs = list_pass.merge(outputs, listed_outputs, collections=collections)
        if vision:
            _confirm_by_vision(outputs, document_ids, rep, model_id=model_id, provider=choice.provider)
        labelled: list[tuple[str, ExtractionOutput]] = []
        for out in outputs:
            stamp_provenance(out, lambda fact: versions.get(_document_of(fact)))
            labelled.append((_label_of(out), out))
        apply_outputs(model, labelled, rep)

    rep.cost_usd = ledger.total_usd
    rep.wall_clock_s = ledger.wall_clock_s
    rep.cost_summary = ledger.summary()
    rep.material_not_stated = sorted(general_report.material_not_stated)
    if general_report.material_unread:
        rep.extractor_flags.append(
            f"corpus: the material read failed on {general_report.material_unread} of "
            f"{general_report.windows} windows; their open read stands"
        )
    # The corpus report's own counts, kept as flags rather than folded into the
    # extractor counters: "23 windows, 2 contested" is not the same kind of
    # number as "the ITS extractor raised 2 flags", and merging them would
    # report one as the other.
    rep.extractor_flags.append(
        f"corpus: {general_report.windows} windows, {general_report.cache_hits} cached, "
        f"{general_report.facts} facts, {general_report.flagged} flagged, "
        f"{general_report.contested} contested, "
        f"{general_report.dropped_invalid_path} invalid paths, "
        f"{general_report.dropped_bad_line} bad lines, "
        f"{general_report.dropped_unverified} unverified"
    )
    if general_report.windows_unread:
        # Its OWN flag, at the front, because it is a different class of thing
        # from the counters above. Those describe what the read found; this says
        # part of the deal was not read at all, and an analyst has to know that
        # before trusting an absence.
        rep.extractor_flags.insert(
            0,
            f"corpus: {general_report.windows_unread} of {general_report.windows} windows could not be read",
        )
    if general_report.support_checked:
        rep.extractor_flags.append(
            f"corpus support: {general_report.support_checked} checked, "
            f"{general_report.support_refuted} refuted, "
            f"{general_report.support_unclear} unclear"
        )
    return rep


def _outside_primary_blocks(primary_blocks: dict[str, set[str]]) -> Callable[[str, str], bool]:
    """True when a fact's block is not one its own document primarily feeds.

    NOT a correctness test, and it must not be read as one. A valuation report
    stating the approved GFA is a fact in the right field from a document the
    taxonomy does not list as that block's primary source — and capturing it is
    the whole reason this engine exists, because no registry extractor can.

    It is a COST decision: which facts are worth a model call. Measured across
    three deal folders on 2026-09-23, this is where a real misassignment would
    hide, because the deterministic legs pass it at 0.95 either way.

    A document with no known slot gets every fact checked: unknown provenance
    is the least safe case, not the most.
    """

    def outside(document_id: str, path: str) -> bool:
        blocks = primary_blocks.get(document_id)
        if not blocks:
            return True
        return path.split(".")[0].split("[")[0] not in blocks

    return outside


def _document_of(fact: Fact[Any]) -> str:
    return fact.provenance.document_id if fact.provenance else ""


def _label_of(output: ExtractionOutput) -> str:
    """The document an output belongs to.

    `extract_corpus` groups by document and sorts, so every fact in one output
    names the same one; the first is enough.
    """
    for fact in output.fields.values():
        doc = _document_of(fact)
        if doc:
            return doc
    return "corpus"


def _extract_with_page_text(extractor, document_id: str, path: Path, page_texts):
    """Run one extractor with its document's page text in context.

    The fact builders score each value against the page it was quoted from, and
    reach that text through a ContextVar rather than through `add_fact`'s 23
    call sites. `pool()` copies the submitting thread's context per task, so
    setting it inside the task keeps each document's pages its own.
    """
    from creditpaper.extractors._doc import page_text_context, pages_from_s2

    with page_text_context(pages_from_s2(page_texts)):
        return extractor.extract(document_id, path, page_texts=page_texts)


def stamp_provenance(
    output: ExtractionOutput,
    ids_for: Callable[[Fact[Any]], tuple[str, str] | None],
) -> None:
    """Write STORAGE identity onto every fact's provenance, in place.

    An extractor knows which page and clause it read; it does not know which
    stored document and version that was, because it was handed a `Path`. The
    ingestion layer does, so it writes `document_id`, `document_version_id` and
    the extractor version here.

    `ids_for` resolves the pair PER FACT rather than per output, which is what
    lets the corpus path share this: its outputs are grouped by document
    already, so each fact names its own document, while a registry extractor's
    whole output came from one file. One function, two shapes, no branch.

    A `model_id` already on the provenance is KEPT. The corpus path records the
    model that actually read the window, and overwriting that with the
    configured default would make the record a guess about itself.
    """
    from creditpaper import __version__

    model_id: str | None = None
    for path, fact in list(output.fields.items()):
        if not fact.provenance:
            continue
        ids = ids_for(fact)
        if ids is None:
            continue
        doc_id, version_id = ids
        is_llm = fact.provenance.method.value == "llm_extraction"
        if is_llm and not fact.provenance.model_id and model_id is None:
            from creditpaper.llm.registry import Task, resolve

            choice = resolve(Task.EXTRACT)
            model_id = f"{choice.provider}:{choice.model}"
        provenance = fact.provenance.model_copy(
            update={
                "document_id": doc_id,
                "document_version_id": version_id,
                "extractor_version": __version__,
                "model_id": fact.provenance.model_id or (model_id if is_llm else None),
            }
        )
        output.fields[path] = fact.model_copy(update={"provenance": provenance})


def apply_outputs(
    model: DealModel, labelled: Sequence[tuple[str, ExtractionOutput]], rep: IngestReport
) -> None:
    """Apply outputs IN THE ORDER GIVEN, then derive and reconcile.

    Split out of `_run_ingest` on 2026-09-23 so the corpus path and the
    per-document registry path write through the SAME code. They produce
    outputs in entirely different ways — nine extractors reading their own
    files, versus one pass over every page of the deal — and the thing that
    must not fork is what happens to a fact afterwards.

    `labelled` is `(label, output)` already in apply order; the label is the
    routing key for a registry extractor and the document for a corpus one, and
    it only ever appears in messages and counts.
    """
    written: set[str] = set()
    for label, out in labelled:
        rep.outputs.append(out)
        # accumulate — a multi-doc slot contributes one output per document
        rep.facts_by_slot[label] = rep.facts_by_slot.get(label, 0) + len(out.fields)
        rep.extractor_flags += [f"{label}: {fl}" for fl in out.flags]

        fresh = ExtractionOutput(fields={p: f for p, f in out.fields.items() if p not in written}, flags=[])
        repeat = ExtractionOutput(fields={p: f for p, f in out.fields.items() if p in written}, flags=[])
        for output_part, apply_mode in ((fresh, ApplyMode.EXTRACT), (repeat, ApplyMode.CONFIRM)):
            if not output_part.fields:
                continue
            result = apply(model, output_part, apply_mode)
            rep.applied += result.applied
            rep.confirmed += result.confirmed
            for path, (held, incoming) in result.divergences.items():
                rep.reconciliation.append(
                    f"divergence [{label}] {path}: documents disagree "
                    f"(held {held!r}, incoming {incoming!r}) — flagged for review"
                )
            rep.reconciliation += [f"apply-reject [{label}] {p}: {why}" for p, why in result.rejected.items()]
            written |= set(result.applied) | set(result.confirmed)

    # Derive (funding identity + aggregates is itself a cross-check) + metrics.
    dr = derive_economics(model)
    derive_metrics(model)
    derive_delivery_model(model)  # construction delivery model (owner-builder / related / independent)
    for path, expected, found in dr.inconsistent:
        rep.reconciliation.append(f"funding_identity: {path} expected {expected} but lines sum to {found}")

    # Named cross-document reconciliation.
    rep.reconciliation += _reconcile(model)


def _run_ingest(
    model: DealModel, todo, registry, max_workers, rep, page_texts_map=None, document_ids=None
) -> None:
    # 1. Parallel extraction — each extractor reads its own document, no model access.
    #
    # `outputs` carries the position each document held in `todo`, because the
    # APPLY ORDER IS LOAD-BEARING and completion order is not deterministic.
    # `apply` creates a keyed collection on first write and CONFIRM upgrades it
    # on the second, so whichever document lands first becomes the primary
    # provenance and the other becomes its confirmation. Collected in
    # `as_completed` order, that was decided by which network call returned
    # first: two runs over identical documents with identical facts could
    # produce different models.
    #
    # `todo` order is the right order, not a sort of the paths — `route_deal`
    # ranks a multi-document slot best-first, so the executed copy precedes the
    # draft. Sorting by path would let the draft claim primary provenance.
    outputs: list[tuple[int, str, object]] = []
    if todo:
        with pool(min(max_workers, len(todo)), thread_name_prefix="ingest") as ex:
            if page_texts_map:
                futs = {
                    ex.submit(
                        _extract_with_page_text,
                        registry[s](),
                        f"{s}:{Path(p).name}",
                        Path(p),
                        page_texts_map.get(Path(p)),
                    ): (i, s, Path(p))
                    for i, (s, p) in enumerate(todo)
                }
            else:
                futs = {
                    ex.submit(registry[s]().extract, f"{s}:{Path(p).name}", Path(p)): (i, s, Path(p))
                    for i, (s, p) in enumerate(todo)
                }
            for fut in as_completed(futs):
                order, slot, source_path = futs[fut]
                try:
                    output = fut.result()
                    if document_ids and source_path in document_ids:
                        ids = document_ids[source_path]
                        # Every fact in this output came from the one file, so
                        # the resolver ignores the fact it is handed.
                        stamp_provenance(output, lambda _fact, ids=ids: ids)
                    outputs.append((order, slot, output))
                except Exception as exc:  # noqa: BLE001 — one doc's failure must not lose the rest
                    rep.failed_documents += 1
                    detail = f"{slot}: extraction failed — {type(exc).__name__}"
                    rep.extractor_flags.append(detail)
                    # ALSO a reconciliation hold, not only a flag. `reconciled` is
                    # defined over `reconciliation` alone, so a failure recorded
                    # only in `extractor_flags` left the run reporting
                    # "reconciliation: clean — nothing held" while an entire
                    # document was missing from the model. Insurance, builder,
                    # building_contract and kyc have no missing-document check
                    # either, so nothing downstream would have caught it.
                    #
                    # This is the path TruncatedResponseError and
                    # ContextWindowExceededError arrive on — the two the choke
                    # point exists to make un-swallowable.
                    rep.reconciliation.append(detail)

    # 2-4. Serial apply (the only write path), derive, reconcile.
    #
    # Back into `todo` order first: see the note above `outputs`. Completion
    # order is not deterministic and apply order decides which document becomes
    # a field's provenance and which becomes its confirmation.
    #
    # The FIRST document to write a path applies it; a LATER document writing
    # the same path CONFIRMS it. Until 2026-09-22 every output was applied with
    # the default EXTRACT, an unconditional `_set_fact`, so two documents
    # disagreeing resolved by apply order, silently, and `result.divergences`
    # was never populated from ingestion.
    apply_outputs(model, [(slot, out) for _, slot, out in sorted(outputs, key=lambda row: row[0])], rep)


# Canonical cost categories, so QS line labels and ITS funding-table keys pair even
# when worded differently. First matching needle wins; unknown keys stay as-is.
_CAT_SYNONYMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("contingency", ("conting",)),
    ("construction", ("construct", "building_work", "build_work")),
    ("professional_fees", ("profession", "consultant")),
    ("statutory_fees", ("statutory", "stat_fee", "authority_fee", "authority_charge")),
    ("preliminaries", ("prelim",)),
    ("interest", ("interest",)),
)


def _canon_cats(key: str) -> list[str]:
    """All canonical categories a key matches. A key matching >1 is a BUNDLED line
    (e.g. 'construction_and_stat_fee_contributions' = construction + stat fees), which
    cannot be paired against a single QS line without inventing a false divergence."""
    k = key.lower()
    return [canon for canon, needles in _CAT_SYNONYMS if any(n in k for n in needles)]


@timed("reconciliation")
def _reconcile(model: DealModel) -> list[str]:
    """Named, finite cross-document checks (D6 philosophy) — surface extraction
    errors/gaps AND missing material documents. A credit paper needs certain documents;
    if one is absent (or its operative version was never identified), that is an explicit
    analyst action ("call for it"), not silence (Jason 2026-06-17). Extended by adding a
    named check, not an open-ended engine."""
    out: list[str] = []
    dt = deal_type_str(model)
    vb = model.valuation.values_by_basis

    # --- Material-document completeness: present, or "obtain and provide" ---------
    its_present = model.economics.facility_amount.value is not None
    val_present = bool(vb)
    if not its_present:
        out.append(
            "missing-document: facility terms (ITS / term sheet / letter of offer) not found "
            "among the provided documents — analyst to obtain and provide"
        )
    if not val_present:
        out.append(
            "missing-document: valuation not found among the provided documents — "
            "analyst to obtain and provide"
        )
    sp = model.sponsor
    sponsor_present = bool(
        sp.financials
        or sp.claims
        or sp.structure.entities
        or sp.structure.trusts
        or sp.cash_equity_to_date.value is not None
        or sp.liquidity_cushion.value is not None
    )
    if not sponsor_present:
        out.append(
            "missing-document: sponsor / borrower financials and track record not found "
            "among the provided documents — analyst to obtain and provide"
        )
    if dt == DealType.CONSTRUCTION.value:
        if model.qs.cost_to_complete.value is None and model.qs.project_cost.value is None:
            out.append(
                "missing-document: QS / cost-to-complete report not found among the provided "
                "documents — analyst to obtain and provide"
            )
        if model.da.reference.value is None:
            out.append(
                "missing-document: planning approval (development consent / permit / decision "
                "notice) not found among the provided documents, or its operative version was "
                "not identified — analyst to obtain and provide"
            )

    # --- Within-document gaps (only meaningful once the document is present) -------
    if val_present:
        aic, asis = vb.get("as_if_complete"), vb.get("as_is")
        if (aic is None or aic.value is None) and "as_if_complete_one_line" not in vb:
            out.append("missing: completed value (LVR denominator) not extracted from the valuation")
        if (
            aic is not None
            and asis is not None
            and aic.value
            and asis.value
            and Decimal(aic.value) < Decimal(asis.value)
        ):
            out.append("value_ordering: as_if_complete < as_is — likely an extraction swap")
    if its_present and not model.covenants:
        out.append("missing: no covenants extracted from the ITS")

    # --- Derivation sanity --------------------------------------------------------
    # Deal type drives day-one advance; without it the day-one LVR silently degrades
    # to facility/as-is (caught on the Riverton full-ingest run, 2026-06-16).
    if model.deal.type.value is None:
        out.append("missing: deal type not classified — day-one advance cannot be derived correctly")
    # A day-one LVR over 100% is facially implausible for a senior facility: the day-one
    # advance has almost certainly defaulted to the full facility. Never let it pass.
    d1 = model.metrics.asis_lvr_pct
    if d1.value is not None and Decimal(d1.value) > 100:
        out.append(
            f"implausible: day-one LVR {d1.value}% > 100% — day-one advance likely "
            "defaulted to full facility; check deal type and land-line senior"
        )

    # --- QS cost lines vs the loan funding table, line by line (check #3) ----------
    # Pair on a CANONICAL category (so 'statutory_fees' pairs with the ITS's
    # 'stat_fees_and_contributions') and surface material per-line divergences — e.g.
    # the contingency line, where the loan funded ~3.8% vs the QS-recommended 10% on
    # the build deal: the line-vs-line reconciliation Jason asked for (2026-06-17).
    # Sum QS amounts per canonical category (a category can span several QS lines);
    # QS lines that are themselves bundled/unknown stay uncategorised.
    # PAIR LIKE WITH LIKE. The funding table's `.total` is TOTAL development cost, so
    # only QS lines read from the total-cost column may be compared to it. A QS report
    # prints several columns — total, costs to date, cost to complete — and they are
    # different quantities.
    #
    # This is the fix for the three divergences reported on 2026-08-13 that were
    # entirely artefacts: the QS construction figure equalled the funding table's
    # SENIOR cell to the cent, because the senior line is sized to fund exactly the
    # remaining construction cost, so QS-cost-to-complete IS funding-senior by
    # construction — and it was being compared against total cost. Professional fees
    # and statutory contributions showed the same signature: nearly all spent before
    # the facility draws. The contingency catch was unaffected and is real, which is
    # the tell — contingency is unspent by definition, so its remaining equals its
    # total.
    #
    # A line whose basis is unstated is NOT paired. The check cannot know what it is
    # looking at, and this comparison's whole value is that its catches are trusted:
    # a bogus catch costs more than a missed one.
    qs_by_cat: dict[str, Decimal] = {}
    unstated_basis: list[str] = []
    for key, cl in model.qs.cost_lines.items():
        if cl.amount.value is None:
            continue
        cats = _canon_cats(key)
        if len(cats) != 1:
            continue
        basis = str(cl.basis.value or "").strip().lower()
        if not basis:
            unstated_basis.append(key)
            continue
        if basis != "total_cost":
            continue  # a different quantity; silently unpairable, not a divergence
        qs_by_cat[cats[0]] = qs_by_cat.get(cats[0], Decimal("0")) + Decimal(cl.amount.value)
    # Compare each SINGLE-category funding line to its QS category total. A funding line
    # that bundles several categories is NOT paired: comparing a bundle against one QS
    # line invents a false divergence (a bogus "catch" is worse than a missed one — it
    # erodes trust in the very mechanism that wins skeptics). The aggregate funding-
    # identity check still covers totals; line-vs-line stays apples-to-apples.
    for fkey, line in model.economics.funding_table.items():
        if line.total.value is None:
            continue
        cats = _canon_cats(fkey)
        if len(cats) != 1 or cats[0] not in qs_by_cat:
            continue
        cat = cats[0]
        fund_amt, qs_amt = Decimal(line.total.value), qs_by_cat[cat]
        tol = max(Decimal("5000"), abs(fund_amt) * Decimal("0.01"))
        if abs(qs_amt - fund_amt) > tol:
            out.append(
                f"qs_vs_funding: '{cat}' line diverges — QS {qs_amt} (total-cost basis) vs "
                f"funding table[{fkey}] {fund_amt} (delta {qs_amt - fund_amt:+})"
            )

    # An unstated basis is a hold, not a silent skip: the line-vs-line comparison is
    # one of the checks that actually finds things (it produced the contingency
    # shortfall), so a run where it could not execute must say so rather than read as
    # clean.
    if unstated_basis:
        out.append(
            "qs_vs_funding: analyst-confirm — QS cost line(s) "
            f"{sorted(unstated_basis)} state no basis (total cost vs cost to complete), "
            "so they were NOT compared against the funding table. Confirm the column "
            "before relying on the absence of a divergence."
        )

    # Self-consistency: the breakdown should sum to one of the QS's own headline
    # figures. Reconciling to NEITHER means the lines were read from mixed columns —
    # the failure that produced the phantom divergences — and it is detectable from
    # the QS report alone, without reference to the funding table.
    line_sum = sum(
        (Decimal(cl.amount.value) for cl in model.qs.cost_lines.values() if cl.amount.value is not None),
        Decimal("0"),
    )
    headlines = {
        "project_cost": model.qs.project_cost,
        "cost_to_complete": model.qs.cost_to_complete,
    }
    known = {name: Decimal(f.value) for name, f in headlines.items() if f.value is not None}
    if line_sum and known:
        tol = max(Decimal("5000"), line_sum * Decimal("0.01"))
        if all(abs(line_sum - total) > tol for total in known.values()):
            stated = ", ".join(f"{n} {v}" for n, v in sorted(known.items()))
            out.append(
                f"qs_self_consistency: cost lines sum to {line_sum}, which matches neither "
                f"{stated} — the breakdown may mix columns (total cost vs cost to complete)"
            )

    # --- Planning approval: off-document facts → analyst-confirm (check #13) -------
    # Where an approval WAS identified, build-readiness and currency are facts no
    # jurisdiction reliably prints on the instrument — confirm rather than assume good.
    da = model.da
    if da.reference.value is not None:
        if da.build_ready.value is not True:
            # Either a genuinely not-build-ready approval (deferred/NOD/preliminary/
            # consent-only) OR a secondary document (e.g. an amendment letter) standing in
            # for the operative consent — both mean the operative permit needs obtaining.
            out.append(
                "analyst-confirm: planning approval not confirmed build-ready — obtain and "
                "confirm the OPERATIVE consent/permit (this may be a deferred-commencement/"
                "NOD/preliminary/consent-only approval, or a secondary document such as an "
                "amendment letter rather than the consent itself)"
            )
        if da.approval_current.value is not True:
            out.append(
                "analyst-confirm: DA currency unconfirmed — confirm the consent has not "
                "lapsed and has lawfully commenced (a site fact, not on the instrument)"
            )
    return out
