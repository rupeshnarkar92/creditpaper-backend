"""General extraction, wired to the model's single write path.

Everything this module composes was built and measured separately — corpus
planning, line anchoring, asserted anchors, multi-pass consensus, the fact
cache — and none of it had a production caller. This is the caller.

It emits `ExtractionOutput` per document, which is the contract `model/apply.py`
already takes, so the result flows through the existing write path with its
keyed-collection creation, its CONFIRM upgrade and its version bump untouched.
Nothing here writes to a `DealModel`.

THE SHAPE

    pages -> plan_corpus       prefilter, dedupe the read, window
          -> fact_cache.get    a window read before replays; no model call
          -> N harvest passes  the model returns a path, a value and a LINE
          -> consensus.merge   union the draws, count the agreement
          -> fact_cache.put    store the MERGED result, so N passes are paid once
          -> clause_at         cut the clause at the cited line; the anchor is ours
          -> verify_located    assert that anchor, check the value is in it
          -> fan_out           one fact per document the content appears in
          -> ExtractionOutput  grouped by document, for apply()

WHAT IS DELIBERATELY NOT HERE

The nine per-document extractors still exist and still run. This is an
ADDITIONAL path, not a replacement, because every number behind it comes from
one deal folder and "never tune on one deal" is the standing rule. Deleting
`EXTRACTOR_REGISTRY` is a separate, reversible step once the general path has
been measured on a second folder.

OCR is not handled here either, and must not be: `Page.text` is whatever the
caller parsed. Built from the `page_texts_map` that `deals/utils/ingest.py`
already receives, OCR comes through the existing processing pipeline. Built
from a bare PDF read, the ~10% of pages with no text layer are silently absent.
`pages_from_map` is the safe constructor for that reason.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from creditpaper.concurrency import pool
from creditpaper.ingest import consensus, fact_cache
from creditpaper.ingest.corpus import Page, ReadWindow, clause_at, fan_out, plan_corpus
from creditpaper.ingest.harvest_prompt import field_kinds, normalise_key_path
from creditpaper.ingest.material_pass import MaterialReader, fold
from creditpaper.ingest.support import SupportChecker
from creditpaper.ingest.tables import TableRow, rows_of_table_at
from creditpaper.ingest.values import figures
from creditpaper.ingest.verify import Reason, Verdict, verify_located
from creditpaper.model.apply import ExtractionOutput
from creditpaper.model.citations import is_valid_path, safe_slug
from creditpaper.model.concepts import Concept, concept_for
from creditpaper.model.core import ExtractionMethod, Fact, FactStatus, Provenance

log = logging.getLogger(__name__)

# A window of 2 pages, measured 2026-09-22: 2.5x the recall of 10 pages and
# anchor divergence down from 25.6% to 6.7%, for 1.9x the cost.
GENERAL_WINDOW = 2

# Concurrent draws. A draw waits on a network, so this is bounded by what
# the provider will take rather than by cores.
DEFAULT_WORKERS = 24

# How many windows may fail with NONE having succeeded before the run gives
# up. Comfortably more than a run could plausibly open with by bad luck, and
# far less than the ~1,055 windows a deal costs - the number only has to sit
# between 'unlucky' and 'ruinous'.
ABORT_AFTER_FAILURES = 20

# How agreement across passes moves confidence.
#
# With ONE pass every fact is unanimous by definition, so single-pass behaviour
# is exactly what it was and multi-pass is purely additive.
#
# `config.EXTRACTION_CONFIDENCE_THRESHOLD` is 0.7, so the bands are chosen to
# mean something: a fact every pass found applies, a majority applies, and a
# fact ONE pass of three found goes to review. That is the point of counting.
UNANIMOUS_CONFIDENCE = 0.95
MAJORITY_CONFIDENCE = 0.85
MINORITY_CONFIDENCE = 0.65

# A harvester takes the rendered window and returns raw fact dicts. Injected so
# this module is testable without a model, and so the caller owns the prompt,
# the model id and the token budget.
Harvester = Callable[[str], list[dict[str, Any]]]

# Builds the semantic support check for one field. Same shape as
# `commit.CheckerFactory`, so one factory serves both ingestion paths.
CheckerFactory = Callable[[str, Concept], SupportChecker]

# Is THIS fact, from THIS document, worth a model call? Injected, because the
# engine cannot know it: the signal is which blocks a document's slot feeds,
# and that is taxonomy the engine deliberately does not import. Returning None
# falls back to the concept registry's own `material` flag.
MaterialFor = Callable[[str, str], bool]


class ExtractionAborted(RuntimeError):
    """Nothing could be read, so the run stopped rather than report an empty deal.

    Raised rather than returned. An empty `DealModel` committed from a run where
    every call failed is the worst outcome available: it is indistinguishable
    from a deal whose documents state nothing, and it would mark the deal
    extracted so nothing would ever read it again.
    """


class _Breaker:
    """Trips when many windows have failed and none has succeeded.

    One success is enough to untrip it permanently: it proves the credentials,
    the network and the account are fine, so every failure after that is a
    property of its own window and belongs in `windows_unread`.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._failures = 0
        self._any_success = False
        self.last: Exception | None = None

    @property
    def tripped(self) -> bool:
        with self._lock:
            return not self._any_success and self._failures >= self._limit

    def failed(self, exc: Exception) -> None:
        with self._lock:
            self._failures += 1
            self.last = exc

    def succeeded(self) -> None:
        with self._lock:
            self._any_success = True


class GeneralReport(BaseModel):
    windows: int = 0
    cache_hits: int = 0
    passes: int = 1
    facts: int = 0
    contested: int = 0  # passes read one path to different values
    unanimous: int = 0
    flagged: int = 0
    dropped_invalid_path: int = 0
    dropped_bad_line: int = 0
    # A number or date field whose value is prose that cannot be typed (a relative
    # date, a range, "TBC"): never a guessed figure, never passed on untyped.
    dropped_untyped: int = 0
    dropped_unverified: int = 0
    # Windows that could not be READ at all. Counted, never silent: an
    # unread window is indistinguishable in the output from a window that
    # states nothing, and that is the one confusion this design refuses.
    windows_unread: int = 0
    # The semantic leg: how many facts were worth a model call, and what it
    # said. `support_refuted` is the misassignment rate this design could not
    # see until 2026-09-23 - a value really in its quote, but not stated OF
    # THIS FIELD.
    support_checked: int = 0
    support_refuted: int = 0
    support_unclear: int = 0
    # Triaged as worth a model call, and no concept names the path so none
    # could run. This is the honest size of the verification gap: the concept
    # registry covers 40% of the schema, and these facts carry the
    # deterministic legs only.
    support_unavailable: int = 0
    # Facts added by reading a table COLUMN rather than the one cell the
    # model pointed at. They cost no model call and are the same on every
    # read, which is the point.
    rows_enumerated: int = 0
    # The material pass (spec §3.3): which material concepts were asked, which
    # some window stated, and how many windows' material read failed. A failed
    # material read keeps the open draw; the window is still read.
    material_asked: set[str] = set()
    material_stated: set[str] = set()
    material_unread: int = 0

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hits / self.windows if self.windows else 0.0

    @property
    def material_not_stated(self) -> set[str]:
        """Material concepts every window was asked about and none stated."""
        return self.material_asked - self.material_stated


def pages_from_map(
    page_texts_map: dict[Path, list[dict[str, Any]]],
    document_ids: dict[Path, tuple[str, str]] | None = None,
) -> list[Page]:
    """`Page`s from the parsed page text the ingest pipeline already carries.

    This is the safe constructor. The map comes from `processing/extractors`,
    which does OCR, so a scanned page arrives with text. Building `Page`s from
    a bare PDF read instead drops the ~10% of pages that have no text layer,
    silently and with no sign in the report.
    """
    out: list[Page] = []
    for path, entries in page_texts_map.items():
        document_id = document_ids[path][0] if document_ids and path in document_ids else str(path)
        for entry in entries or []:
            try:
                number = int(entry.get("page", 0) or 0)
            except (TypeError, ValueError):
                continue
            if number > 0:
                out.append(
                    Page(
                        document_id=document_id,
                        page=number,
                        text=str(entry.get("text") or ""),
                        # `processing/extractors/pdf.py` sets this when the page
                        # had no usable text layer and was read by Tesseract. A
                        # page a vision read AGREED with is no longer machine-read
                        # (`processing/vision.py`): two independent reads stand behind it.
                        ocr_used=bool(entry.get("ocr_used")) and not entry.get("ocr_agrees"),
                    )
                )
    return out


def typed_value(raw: object, kind: str) -> object:
    """A returned value parsed to the type its field DECLARES.

    `_doc.add_fact` guesses instead -- strip `$ , %`, try Decimal, fall back to
    a string -- so a licence number arrives as a Decimal and
    `apply._coerce_to_declared` has to convert it back. Worse in the other
    direction: a money field receiving an unparsed `"12450000"` is REFUSED by
    apply, correctly, because a string in a Decimal field is an unparsed
    extraction rather than something to coerce quietly.

    Knowing the kind removes both guesses.
    """
    if raw is None:
        return None
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().casefold()
        return True if text == "true" else False if text == "false" else raw
    if kind == "number":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, Decimal)):
            return Decimal(raw)
        cleaned = str(raw).replace(",", "").replace("$", "").replace("%", "").strip()
        try:
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            pass
        # As the document wrote it ("approx. 450 sqm", "$1.25m"): the tokeniser
        # the verifier uses, when it finds exactly ONE figure. Two figures ("between
        # $1m and $2m") or none is prose, a real extraction failure: left as written
        # so it is never a guessed figure.
        numbers = [f for f in figures(str(raw)) if f.kind.value in ("money", "number", "percent")]
        if len(numbers) == 1:
            try:
                return Decimal(numbers[0].value)
            except (InvalidOperation, ValueError):
                pass
        return str(raw)
    if kind == "date":
        if isinstance(raw, date):
            return raw
        try:
            return date.fromisoformat(str(raw).strip())
        except ValueError:
            pass
        # "12 March 2027", "12/03/2027" (day first): exactly one FULL date. A month
        # ("March 2027") or a relative date ("20 Business Days after ...") is not one.
        dates = [f.value for f in figures(str(raw)) if f.kind.value == "date" and len(f.value) == 10]
        if len(dates) == 1:
            try:
                return date.fromisoformat(dates[0])
            except ValueError:
                pass
        return str(raw)
    return str(raw)


def fits_schema(path: str, value: object) -> bool:
    """Whether the model's single write path would accept `value` at `path`: the
    check the ledger makes, run on one fact so a refusal costs that fact, not the deal."""
    from creditpaper.model import DealModel
    from creditpaper.model.apply import apply

    probe = Fact(
        value=value,
        provenance=Provenance(
            document_id="probe", page=1, location="probe", method=ExtractionMethod.LLM_EXTRACTION
        ),
        confidence=0.5,
        status=FactStatus.EXTRACTED,
    )
    return not apply(
        DealModel(tenant_id="probe", deal_id="probe"), ExtractionOutput({path: probe}, [])
    ).rejected


def is_typed(value: object, kind: str | None) -> bool:
    """Whether `value` has the type its declared kind needs: a string in a number
    or date field is an untyped extraction, which the ledger refuses outright."""
    if kind == "number":
        return isinstance(value, (Decimal, int, bool))
    if kind == "date":
        return isinstance(value, date)
    return True


# The last `[key]` in a path: the collection the field belongs to. In
# `check_valuations[a].comp_set[b].sale_price` that is `[b]`, the comparable,
# not the check valuation holding it.
_LAST_KEY = re.compile(r"\[[^\[\]]*\](?=[^\[\]]*$)")


def _column_rows(
    path: str, value: object, page_text: str, span: tuple[int, int]
) -> list[tuple[str, str, TableRow]]:
    """The whole COLUMN the cited value sits in, as `(path, cell, row)`.

    The fix for the largest source of instability in this design. A window
    holding twelve comparable sales gets three or five reported, a different
    three or five next time, so the same documents make a different fact sheet;
    372 of 404 distinct paths measured on 2026-09-24 were collection rows and
    that is where the flapping lived. More passes, a stronger model and key
    canonicalisation all failed to move it, because none of them changes what
    the model is asked to do: CHOOSE which rows to mention.

    So it chooses one cell and this reads the rest of the column. Two reads
    that mention different rows of one table now produce the same set, and it
    costs no model call at all.

    Returns nothing unless every step is PROVEN: the path names a collection,
    the span is a table row, the value is one of that row's cells (so the
    column is positional and known), and the table has more than one row.
    Anything less and the caller keeps exactly the fact the model gave it.
    """
    if "[" not in path:
        return []
    rows = rows_of_table_at(page_text, span[0], span[1])
    if len(rows) < 2:
        return []
    wanted = figures(str(value))
    if len(wanted) != 1:
        # A value that is not a single figure cannot name a column positionally.
        return []
    cited = next((r for r in rows if r.start <= span[0] < r.end), None)
    if cited is None:
        return []
    column = next((n for n, c in enumerate(cited.cells) if c.value == wanted[0].value), None)
    if column is None:
        return []
    out: list[tuple[str, str, TableRow]] = []
    for row in rows:
        if column >= len(row.cells):
            continue
        key = safe_slug(row.label)
        if not key:
            continue
        out.append((_LAST_KEY.sub(f"[{key}]", path), row.cells[column].value, row))
    return out


def _confidence(fact: consensus.ConsensusFact) -> float:
    if fact.agreement >= 1.0:
        return UNANIMOUS_CONFIDENCE
    if fact.agreement >= 0.5:
        return MAJORITY_CONFIDENCE
    return MINORITY_CONFIDENCE


def merge_draws(draws: Sequence[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """N draws of one window, unioned, in the shape the fact cache stores.

    PUBLIC because the determinism measurement has to union exactly as
    production does. Measuring the stability of a SINGLE draw answers a
    question nobody asks: what the cache freezes, and what every later read
    replays, is the merged result. The same rule that moved the harvester into
    `src/` - the measured path is the shipped path.

    Union, not intersection: a fact one pass of three saw is kept and carries
    its agreement, because `_confidence` turns that into a confidence band and
    `EXTRACTION_CONFIDENCE_THRESHOLD` sends the weakest to review. Intersecting
    would discard the recall that reading three times buys.
    """
    return [
        {
            "path": f.path,
            "value": f.value,
            "line": f.line,
            "seen_in": f.seen_in,
            "passes": f.passes,
            "conflicting": list(f.conflicting),
        }
        for f in consensus.merge(list(draws))
    ]


def _draw(
    window: ReadWindow,
    harvester: Harvester,
    *,
    passes: int,
    tenant_id: str,
    deal_id: str,
    model_id: str,
    use_cache: bool,
    material_reader: MaterialReader | None = None,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """The merged draw for one window, whether the cache supplied it, and whether its material read failed.

    An empty list is a real answer — "this window states nothing in the field
    list" — and roughly half the corpus says it, so a cached empty must not be
    re-read as a miss.
    """
    if use_cache:
        stored = fact_cache.get(tenant_id, deal_id, window.content_hash, model_id=model_id)
        if stored is not None:
            return stored, True, False

    text = window.numbered()
    merged = merge_draws([harvester(text) for _ in range(max(1, passes))])
    material_failed = False
    if material_reader is not None:
        try:
            merged = fold(merged, material_reader(text), passes=max(1, passes))
        except Exception as exc:  # the open draw stands; the window is still read
            log.warning("material read failed for window %s: %s", window.content_hash[:8], type(exc).__name__)
            material_failed = True
    if use_cache and not material_failed:
        # The MERGED result is stored, so N passes are paid for once per deal
        # and every later ingestion replays the same consensus. A draw missing
        # its material answers is not stored: cached, it would never be asked again.
        fact_cache.put(tenant_id, deal_id, window.content_hash, merged, model_id=model_id)
    return merged, False, material_failed


def extract_corpus(
    pages: Iterable[Page],
    harvester: Harvester,
    *,
    tenant_id: str,
    deal_id: str,
    model_id: str,
    passes: int = 1,
    window: int = GENERAL_WINDOW,
    use_cache: bool = True,
    reference: str | None = None,
    checker_factory: CheckerFactory | None = None,
    material_for: MaterialFor | None = None,
    workers: int = DEFAULT_WORKERS,
    material_reader: MaterialReader | None = None,
) -> tuple[list[ExtractionOutput], GeneralReport]:
    """Read the whole corpus and return one `ExtractionOutput` per document.

    Per document, not per window, because `apply` decides EXTRACT versus
    CONFIRM by which document a fact came from: a second document agreeing
    upgrades a field to `confirmed`, and one that diverges flags it. Facts
    grouped per window would present one document as many and break that.
    """
    plan = plan_corpus(pages, window=window)
    report = GeneralReport(windows=plan.calls, passes=max(1, passes))

    # The SAME generated reference that built the prompt also says what type
    # each field declares, so the prompt and the parse cannot disagree about
    # whether a field holds a number.
    kinds = field_kinds(reference) if reference else {}

    by_document: dict[str, dict[str, Fact[Any]]] = {}
    flags: dict[str, list[str]] = {}

    # THE DRAWS RUN CONCURRENTLY. Everything after them stays serial and in
    # PLAN ORDER, because `apply` decides a field's provenance by which
    # document writes it first, so which call returned first must not matter.
    #
    # This was serial end to end until 2026-09-25: a 1,055-window corpus made
    # 1,055 sequential model calls, roughly 35 minutes before a fact reached
    # the analyst. Every timing in the spec came from the probe, which has its
    # own pool, so the shipped path was far slower than anything measured - the
    # fourth time a measurement scored a pipeline production does not run.
    #
    # A draw waits on a network, so the pool is bounded by what the provider
    # will take, not by cores.
    # A window that fails on its own content is survivable. A window that fails
    # because the API key is dead, the account is out of credit or the provider
    # is down is NOT: that failure hits every window, so catching it per window
    # converts a hard stop into a deal reported as read and empty - after paying
    # for a thousand doomed calls on the way. The breaker tells the two apart by
    # the only signal that actually separates them: whether anything at all has
    # been read yet.
    failures = _Breaker(limit=ABORT_AFTER_FAILURES)

    def _drawn(read_window: ReadWindow) -> tuple[list[dict[str, Any]], bool, bool] | None:
        """One window's draw, or None where it could not be read.

        `executor.map` re-raises, so before this a SINGLE unreadable window took
        the whole ingestion down with it - one document truncating the model's
        response aborted a 147-document run that was otherwise complete. A deal
        is worth more read with one window missing and said so than not read.

        The catch is deliberately broad. Narrowing it to the exceptions seen so
        far only means the next new one brings the run down again, and the
        environmental case that a narrow catch would have handled is covered by
        the breaker instead - by evidence rather than by exception type, which
        does not need a list of provider error classes kept up to date.
        """
        if failures.tripped:
            return None
        try:
            drawn = _draw(
                read_window,
                harvester,
                passes=passes,
                tenant_id=tenant_id,
                deal_id=deal_id,
                model_id=model_id,
                use_cache=use_cache,
                material_reader=material_reader,
            )
        except Exception as exc:
            log.warning("window %s could not be read: %s", read_window.content_hash[:8], type(exc).__name__)
            failures.failed(exc)
            return None
        failures.succeeded()
        return drawn

    with pool(max(1, workers), thread_name_prefix="corpus-window") as executor:
        drawn = list(executor.map(_drawn, plan.windows))

    # Nothing at all could be read. That is not a deal that states nothing, and
    # it must not be committed as one - so it raises here rather than returning
    # an empty result the caller would write to the model and mark extracted.
    if plan.windows and all(outcome is None for outcome in drawn):
        raise ExtractionAborted(
            f"no window of {len(plan.windows)} could be read"
            + (f": {type(failures.last).__name__}" if failures.last else "")
        ) from failures.last

    for read_window, outcome in zip(plan.windows, drawn, strict=True):
        if outcome is None:
            report.windows_unread += 1
            continue
        raw, hit, material_failed = outcome
        if hit:
            report.cache_hits += 1
        if material_failed:
            report.material_unread += 1

        for item in raw:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            value = item.get("value")
            marker = item.get("material")
            if marker in ("stated", "not_stated") and path:
                report.material_asked.add(path)
                if marker == "stated":
                    report.material_stated.add(path)
            if not path or value is None:
                continue
            if not is_valid_path(path):
                # The citation grammar is the contract; a malformed path cannot
                # be applied and must not be counted as a fact.
                report.dropped_invalid_path += 1
                continue

            line = item.get("line")
            located = clause_at(read_window, line) if isinstance(line, int) else None
            if located is None:
                report.dropped_bad_line += 1
                continue

            kind = kinds.get(normalise_key_path(path), "text") if kinds else None
            if kind is not None:
                value = typed_value(value, kind)
                if not is_typed(value, kind):
                    # Prose in a number or date field ("20 Business Days after ..."):
                    # dropped and counted HERE. Passed on, the ledger refuses the
                    # whole deal's commit over it.
                    report.dropped_untyped += 1
                    continue
            if kinds and not fits_schema(path, value):
                # Any other value its field refuses (an enum given a meaning it does
                # not have): the same refusal, caught here for the same reason.
                report.dropped_untyped += 1
                continue

            checkable = _value_is_checkable(value)
            # The semantic leg is PER FACT and it spends, so it is chosen per
            # fact. `material_for` is the caller's signal (for the corpus path,
            # a fact landing outside the blocks its document feeds); the
            # concept registry's own `material` flag is the floor beneath it.
            concept = concept_for(path) if checker_factory is not None else None
            checker = checker_factory(path, concept) if checker_factory and concept else None
            material = bool(material_for(read_window.document_id, path)) if material_for else False
            if concept is not None and concept.material:
                material = True
            # The PAGE TEXT and raw offsets, not the cut span:
            # `value_in_page_span` reads figures from the whole page and keeps
            # those inside the span, because a span read out of context can say
            # something the page does not ("Lot 5 June 2027" cut to a date).
            verification = verify_located(
                value=value,
                page_text=read_window.texts_by_page()[located.page],
                page=located.page,
                span=located.span,
                check_value=checkable,
                support_checker=checker,
                material=material,
                # Capped if it stands on OCR: the text is a reading of the page,
                # not the page. Measured 2026-09-25, facts read off OCR'd pages
                # are markedly less stable and the model cannot adjudicate them.
                ocr_text=located.page in read_window.ocr_pages,
            )
            if Reason.SUPPORT_UNAVAILABLE in verification.reasons:
                report.support_unavailable += 1
            if verification.support is not None:
                report.support_checked += 1
                if verification.support.supports == "no":
                    report.support_refuted += 1
                elif verification.support.supports == "unclear":
                    report.support_unclear += 1

            seen_in = item.get("seen_in")
            total = item.get("passes")
            merged_fact = consensus.ConsensusFact(
                path=path,
                value=value,
                line=line if isinstance(line, int) else None,
                seen_in=int(seen_in) if isinstance(seen_in, int) else 1,
                passes=int(total) if isinstance(total, int) else max(1, passes),
                conflicting=tuple(item.get("conflicting") or ()),
            )

            if verification.verdict in (Verdict.VERIFIED, Verdict.ANCHORED):
                # The LOWER of the two. Consensus confidence says how steadily
                # the passes saw this fact; the verifier's says what the legs
                # proved, and a support answer of "unclear" caps it. Taking the
                # consensus number alone would discard that cap - three passes
                # agreeing on a fact the model cannot confirm is agreement
                # about an unconfirmed fact.
                confidence = min(_confidence(merged_fact), verification.confidence)
                status = FactStatus.EXTRACTED
            else:
                # The value is not on the line the model cited. That is the one
                # failure line anchoring leaves, and it is the one worth
                # keeping: it catches the model pointing at the wrong place.
                confidence = verification.confidence
                status = FactStatus.FLAGGED
                report.dropped_unverified += 1

            if merged_fact.contested:
                # Passes read the same line to different values. A single pass
                # would have reported one of them looking certain.
                status = FactStatus.FLAGGED
                report.contested += 1
            if merged_fact.unanimous:
                report.unanimous += 1
            if status is FactStatus.FLAGGED:
                report.flagged += 1

            # THE WHOLE COLUMN, not just the cell the model pointed at.
            # Deterministic, free, and the same set on every read.
            siblings = (
                _column_rows(path, value, read_window.texts_by_page()[located.page], located.line_span)
                if status is not FactStatus.FLAGGED
                else []
            )

            for location in fan_out(read_window, located.page):
                for row_path, cell, row in siblings:
                    row_value = typed_value(cell, kind) if kind else cell
                    if kind and (
                        not is_typed(row_value, kind) or (kinds and not fits_schema(row_path, row_value))
                    ):
                        # A cell its field would refuse (an OCR glyph in a money
                        # column): dropped like any value, never passed on.
                        report.dropped_untyped += 1
                        continue
                    by_document.setdefault(location.document_id, {})[row_path] = Fact(
                        value=row_value,
                        status=FactStatus.EXTRACTED,
                        confidence=confidence,
                        verification=verification.summary(),
                        provenance=Provenance(
                            document_id=location.document_id,
                            page=location.page,
                            location=read_window.texts_by_page()[located.page][row.start : row.end][:500],
                            method=ExtractionMethod.LLM_EXTRACTION,
                            model_id=model_id,
                        ),
                    )
                    report.facts += 1
                    report.rows_enumerated += 1

                if siblings:
                    # The cited row is IN the enumeration, under a key derived
                    # from the row's own label. Keeping the model's fact too
                    # would keep its arbitrary key - `[riddell]` where the page
                    # says "12 Riddell Street" - and that key is exactly what
                    # flaps between reads. The enumerated set is the fact.
                    continue

                fact: Fact[Any] = Fact(
                    value=value,
                    status=status,
                    confidence=confidence,
                    # Why the fact stands where it does (an OCR cap, a support answer):
                    # targeted vision, the review queue and the ledger read it.
                    verification=verification.summary(),
                    provenance=Provenance(
                        document_id=location.document_id,
                        page=location.page,
                        location=located.text[:500],
                        method=ExtractionMethod.LLM_EXTRACTION,
                        model_id=model_id,
                    ),
                )
                by_document.setdefault(location.document_id, {})[path] = fact
                report.facts += 1
                if merged_fact.contested:
                    flags.setdefault(location.document_id, []).append(
                        f"{path}: passes disagreed on the value; held "
                        f"{value!r} over {len(merged_fact.conflicting)} other reading(s)"
                    )

    # Sorted, so two runs over the same corpus apply in the same order and
    # `apply` creates keyed collections identically. Apply order decides which
    # document becomes a field's provenance and which becomes its confirmation.
    outputs = [
        ExtractionOutput(fields=by_document[doc], flags=flags.get(doc, [])) for doc in sorted(by_document)
    ]
    return outputs, report


def _value_is_checkable(value: object) -> bool:
    """Whether `value_in_span` can apply — production's note/fact split.

    A BOOLEAN ("true" is the model's reading of "the licence is current") and a
    SUMMARY (a paraphrase of the sentence it came from) cannot appear in their
    own quote by construction, and `_doc.add_note_fact` verifies those on the
    anchor alone. Same rule, same threshold.
    """
    text = str(value).strip()
    if text.casefold() in {"true", "false"}:
        return False
    return len(text.split()) <= 5


def documents_in(outputs: Sequence[ExtractionOutput]) -> int:
    return len(outputs)
