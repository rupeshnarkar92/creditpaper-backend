"""The persistent fact write boundary.

LLM work finishes before entering here. Each write locks the deal in Postgres,
validates through model.apply, records evidence and decisions, and refreshes the
DealModel projection in one transaction. No last-writer-wins conflict resolution.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from creditpaper.db import tenant_cursor, tenant_uuid
from creditpaper.ingest.compare import DiffClass, classify, source_key
from creditpaper.ledger import contradictions
from creditpaper.ledger.labels import label_from_assertion, record_label
from creditpaper.ledger.types import (
    RECOMMENDATION_REQUIRED_MESSAGE,
    ApprovalContradictionsNotAcknowledged,
    ApprovalDisclosure,
    ApprovalDisclosuresNotAcknowledged,
    ApprovalDocumentBlocker,
    ApprovalDocumentsNotReady,
    ApprovalGuardrailsNotSignedOff,
    Conflict,
    FactPeriod,
    Hold,
    NotFound,
    PaperSectionBlocker,
    PaperSectionsNotReady,
    QueryScope,
    RecommendationRequired,
)
from creditpaper.model.apply import ExtractionOutput, apply
from creditpaper.model.citations import resolve_path
from creditpaper.model.concepts import REGISTRY_VERSION, Concept, concept_for
from creditpaper.model.core import ExtractionMethod, Fact, FactStatus, VerificationSummary
from creditpaper.model.deal import DealModel


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _with_review_reasons(
    fact: Fact,
    *,
    low_confidence: bool,
    unscored: bool,
    differs: bool,
    contradicted: bool,
) -> tuple[Fact, tuple[str, ...]]:
    """Attach machine-readable hold causes to the immutable assertion payload.

    Labels are derived from that payload later, so this preserves the exact reason
    shown at decision time without weakening the conservative review decision.
    """
    reasons = list(fact.verification.reasons if fact.verification else ())
    for active, code in (
        (low_confidence, "LOW_CONFIDENCE"),
        (unscored, "UNSCORED_CONCEPT"),
        (differs, "DIFFERS_FROM_CURRENT"),
        (contradicted, "CONTRADICTION"),
        (fact.status == FactStatus.FLAGGED, "EXTRACTOR_FLAGGED"),
    ):
        if active and code not in reasons:
            reasons.append(code)
    if not reasons:
        return fact, ()
    verification = fact.verification or VerificationSummary(
        verdict="flagged_review",
        confidence=fact.confidence or 0,
    )
    # A ledger hold is what the analyst was actually shown. Do not leave a
    # verifier-level `verified` verdict on a fact that the product held back;
    # doing so incorrectly counts an analyst correction as a false pass.
    verification = verification.model_copy(update={"verdict": "flagged_review"})
    return (
        fact.model_copy(update={"verification": verification.model_copy(update={"reasons": tuple(reasons)})}),
        tuple(reasons),
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def facts_in(obj: Any, path: str = "") -> dict[str, Fact]:
    if isinstance(obj, Fact):
        return {path: obj} if obj.value is not None and obj.provenance else {}
    out: dict[str, Fact] = {}
    if isinstance(obj, BaseModel):
        for key in type(obj).model_fields:
            out.update(facts_in(getattr(obj, key), f"{path}.{key}" if path else key))
    elif isinstance(obj, dict):
        for key, item in obj.items():
            out.update(facts_in(item, f"{path}[{key}]"))
    return out


def lock_deal(cur, tenant_id: str, deal_id: str, expected: int | None = None) -> int:
    cur.execute(
        "SELECT public.lock_ledger_deal(%s,%s)",
        (tenant_id, deal_id),
    )
    row = cur.fetchone()
    if not row:
        raise NotFound("Deal not found")
    if expected is not None and row[0] != expected:
        raise Conflict("Deal changed; reload before submitting the decision")
    return row[0]


def clock(cur) -> datetime:
    # clock_timestamp after the row lock; transaction start can precede another writer's commit.
    cur.execute("SELECT public.ledger_clock()")
    return cur.fetchone()[0]


def _history(cur, tenant_id: str, deal_id: str, scope: QueryScope) -> list[dict]:
    cur.execute(
        "SELECT * FROM public.ledger_history_rows(%s,%s,%s,%s,%s)",
        (tenant_uuid(tenant_id), deal_id, scope.known_at, scope.valid_at, scope.include_undated),
    )
    names = [c.name for c in cur.description]
    rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    # A derived assertion is usable only while its exact input assertions are
    # selected at both clocks. Never return a stale ratio after a past correction.
    while True:
        selected = {str(r["assertion_id"]) for r in rows}
        kept = [
            r
            for r in rows
            if r["fact"]["provenance"]["method"] != "derived"
            or (
                r["fact"]["provenance"].get("input_assertion_ids")
                and set(r["fact"]["provenance"]["input_assertion_ids"]).issubset(selected)
            )
        ]
        if len(kept) == len(rows):
            return rows
        rows = kept


def read_facts(tenant_id: str, deal_id: str, scope: QueryScope | None = None) -> list[dict]:
    scope = scope or QueryScope.now()
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT public.ledger_deal_exists(%s,%s)", (tenant_uuid(tenant_id), deal_id))
        if not cur.fetchone()[0]:
            raise NotFound("Deal not found")
        return _history(cur, tenant_id, deal_id, scope)


def _annotate_evidence_tiers(cur, tenant_id: str, deal_id: str, model: DealModel) -> None:
    """Each fact's evidence tier from its document's CURRENT slot (spec §3.8, `model/evidence.py`).

    Derived on read, never stored: a stored tier would go stale when an analyst re-files a document
    (D3), and would change the assertion fingerprint so an unchanged re-extraction wrote a duplicate."""
    from creditpaper.model.evidence import annotate_tiers

    cur.execute(
        """SELECT f.document_id::text, s.slot_key FROM document_slot_files f
        JOIN document_slots s ON s.id = f.slot_id
        WHERE f.tenant_id = %s AND s.deal_id = %s AND f.role = 'assigned'""",
        (tenant_id, deal_id),
    )
    slots_of: dict[str, set[str]] = {}
    for document_id, slot_key in cur.fetchall():
        slots_of.setdefault(document_id, set()).add(slot_key)
    annotate_tiers(model, slots_of)


def _project(
    cur, tenant_id: str, deal_id: str, scope: QueryScope, *, include_derived: bool = True
) -> DealModel:
    projected = DealModel(tenant_id=str(tenant_id), deal_id=deal_id)
    fields = {}
    # An open contradiction on a material concept keeps the concept out of the paper
    # (spec 3a) until an analyst resolves it; its assertions stay in the ledger.
    held = contradictions.open_material_paths(cur, tenant_id, deal_id, scope.known_at)
    for row in _history(cur, tenant_id, deal_id, scope):
        if row["field_path"] in held:
            continue
        value = dict(row["fact"])
        if not include_derived and value["provenance"]["method"] == "derived":
            continue
        value["assertion_id"] = str(row["assertion_id"])
        value["valid_from"], value["valid_to"] = row["valid_from"], row["valid_to"]
        # Restore declared Decimal/date/enum types through the single model write path.
        fields[row["field_path"]] = _typed_fact(row["field_path"], value)
    result = apply(projected, ExtractionOutput(fields=fields, flags=[]))
    if result.rejected:
        raise Hold("Stored facts do not match the current model schema")
    return projected


def _typed_fact(path: str, payload: dict) -> Fact:
    """Parse JSON using the declared Fact[T], including keyed collection entries."""
    from typing import get_args

    cls = DealModel
    for segment in path.split("."):
        name, _, key = segment.partition("[")
        if name not in cls.model_fields:
            raise Hold("Unknown fact field")
        annotation = cls.model_fields[name].annotation
        if key:
            annotation = get_args(annotation)[1]
        cls = annotation
    if not isinstance(cls, type) or not issubclass(cls, Fact):
        raise Hold("Field does not identify a fact")
    return cls.model_validate(payload)


def load_model(tenant_id: str, deal_id: str, scope: QueryScope | None = None) -> DealModel | None:
    resolved_scope = scope or QueryScope.now()
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT public.ledger_revision(%s,%s)", (tenant_uuid(tenant_id), deal_id))
        row = cur.fetchone()
        if row is None or row[0] is None:
            raise NotFound("Deal not found")
        if row[0]:
            projected = _project(cur, tenant_id, deal_id, resolved_scope)
            _annotate_evidence_tiers(cur, tenant_id, deal_id, projected)
            # Spec §3.9: a pure function of the stored text, so applied on read like the tier.
            from creditpaper.ingest.injection import annotate_injection

            annotate_injection(projected)
            if scope is None:
                projected.model_version = row[0]
            else:
                cur.execute(
                    "SELECT public.ledger_outbox_revision(%s,%s,%s)",
                    (tenant_uuid(tenant_id), deal_id, scope.known_at),
                )
                projected.model_version = cur.fetchone()[0]
            return projected
        if scope is not None:
            raise Hold("Legacy snapshots have no reliable temporal history")
        cur.execute("SELECT public.legacy_deal_snapshot(%s,%s)", (tenant_uuid(tenant_id), deal_id))
        old = cur.fetchone()
        if old:
            raw = json.loads(old[0]) if isinstance(old[0], str) else old[0]
            try:
                return DealModel.model_validate(raw)
            except ValueError:
                raise Hold("Legacy snapshot requires re-extraction with traceable sources") from None
        return None


def _select(
    cur,
    tenant_id: str,
    deal_id: str,
    path: str,
    assertion_id: str,
    period: FactPeriod,
    now: datetime,
    actor_id: str | None,
    reason: str,
) -> None:
    """Split overlapping effective intervals; supersede by appending closure events."""
    cur.execute(
        "SELECT * FROM public.ledger_resolution_overlaps(%s,%s,%s,%s,%s)",
        (tenant_id, deal_id, path, period.valid_from, period.valid_to),
    )
    residuals = []
    for old_id, old_assertion, start, end in cur.fetchall():
        cur.execute(
            "SELECT public.record_ledger_supersession(%s,%s,%s,%s,%s,%s)",
            (tenant_id, deal_id, old_id, now, actor_id, reason),
        )
        if period.valid_from is not None and (start is None or start < period.valid_from):
            residuals.append((old_assertion, start, period.valid_from))
        if period.valid_to is not None and (end is None or end > period.valid_to):
            residuals.append((old_assertion, period.valid_to, end))
    for aid, start, end in [*residuals, (assertion_id, period.valid_from, period.valid_to)]:
        cur.execute(
            "SELECT public.record_ledger_resolution(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (tenant_id, deal_id, path, aid, start, end, now, actor_id, reason),
        )


def _assert(
    cur,
    tenant_id: str,
    deal_id: str,
    path: str,
    fact: Fact,
    disposition: str,
    now: datetime,
    run_id: str,
    source_system: str | None = None,
) -> tuple[str, bool]:
    payload = fact.model_dump(mode="json", exclude={"assertion_id"})
    payload["provenance"]["extraction_run_id"] = run_id
    version = fact.provenance.document_version_id if fact.provenance else None
    fingerprint_payload = dict(payload)
    fingerprint_payload["provenance"] = dict(payload.get("provenance") or {})
    fingerprint_payload["provenance"].pop("extraction_run_id", None)
    fingerprint = digest([path, fingerprint_payload, source_system])
    cur.execute(
        "SELECT public.insert_ledger_assertion(%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            tenant_id,
            deal_id,
            path,
            canonical(payload),
            fact.valid_from,
            fact.valid_to,
            now,
            version,
            source_system or (fact.provenance.source_system if fact.provenance else None),
            run_id,
            fingerprint,
            disposition,
        ),
    )
    row = cur.fetchone()
    if row and row[0]:
        return str(row[0]), True
    cur.execute(
        "SELECT public.ledger_assertion_by_fingerprint(%s,%s,%s)",
        (tenant_id, deal_id, fingerprint),
    )
    return str(cur.fetchone()[0]), False


def _bootstrap(cur, tenant_id: str, deal_id: str, now: datetime, run_id: str) -> None:
    cur.execute("SELECT public.ledger_has_assertions(%s,%s)", (tenant_id, deal_id))
    if cur.fetchone():
        return
    cur.execute("SELECT public.legacy_model_snapshot(%s,%s)", (tenant_id, deal_id))
    row = cur.fetchone()
    if not row:
        return
    raw = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    for path, fact in facts_in(DealModel.model_validate(raw)).items():
        # Import time is honest knowledge time. Historical knowledge cannot be reconstructed.
        aid, created = _assert(cur, tenant_id, deal_id, path, fact, "legacy", now, run_id, "legacy_import")
        if created:
            _select(
                cur,
                tenant_id,
                deal_id,
                path,
                aid,
                FactPeriod(valid_from=fact.valid_from, valid_to=fact.valid_to),
                now,
                None,
                "legacy import",
            )


def _refresh(cur, tenant_id: str, deal_id: str, now: datetime, run_id: str) -> DealModel:
    from creditpaper.model import derive_delivery_model, derive_economics, derive_metrics

    scope = QueryScope(valid_at=now, known_at=now, include_undated=True)
    old_derived = {
        r["field_path"]: str(r["assertion_id"])
        for r in _history(cur, tenant_id, deal_id, scope)
        if r["fact"]["provenance"]["method"] == "derived"
    }
    projected = _project(cur, tenant_id, deal_id, scope, include_derived=False)
    derive_economics(projected)
    derive_metrics(projected)
    derive_delivery_model(projected)
    all_facts = facts_in(projected)
    new_derived = set()
    for path, fact in all_facts.items():
        if fact.provenance.method != ExtractionMethod.DERIVED:
            continue
        new_derived.add(path)
        dependencies = [
            f
            for p, f in all_facts.items()
            if p != path
            and f.assertion_id
            and any(
                p == prefix or p.startswith(prefix + ".") or p.startswith(prefix + "[")
                for prefix in fact.provenance.inputs
            )
        ]
        prov = fact.provenance.model_copy(
            update={
                "source_system": "derived",
                "input_assertion_ids": sorted({f.assertion_id for f in dependencies}),
            }
        )
        starts = [f.valid_from for f in dependencies if f.valid_from]
        ends = [f.valid_to for f in dependencies if f.valid_to]
        period = FactPeriod(
            valid_from=max(starts) if starts and len(starts) == len(dependencies) else None,
            valid_to=min(ends) if ends else None,
        )
        fact = fact.model_copy(update={"provenance": prov, **period.model_dump()})
        aid, created = _assert(cur, tenant_id, deal_id, path, fact, "accepted", now, run_id, "derived")
        if created or old_derived.get(path) != aid:
            _select(cur, tenant_id, deal_id, path, aid, period, now, None, "derived from evidence")
        all_facts[path] = fact.model_copy(update={"assertion_id": aid})
        apply(projected, ExtractionOutput({path: all_facts[path]}, []))
    vanished = list(set(old_derived) - new_derived)
    if vanished:
        cur.execute(
            "SELECT public.supersede_derived_resolutions(%s,%s,%s,%s)",
            (tenant_id, deal_id, vanished, now),
        )
    cur.execute(
        "SELECT public.bump_ledger_revision(%s,%s)",
        (tenant_id, deal_id),
    )
    projected.model_version = cur.fetchone()[0]
    cur.execute(
        "SELECT public.save_ledger_snapshot(%s,%s,%s::jsonb)",
        (tenant_id, deal_id, canonical(projected.model_dump(mode="json"))),
    )
    cur.execute(
        "SELECT public.enqueue_ledger_event(%s,%s,%s,%s::jsonb)",
        (tenant_id, deal_id, "facts.changed", canonical({"revision": projected.model_version})),
    )
    return projected


def _source(fact: Fact) -> str | None:
    return source_key(fact)


def _peers(cur, tenant_id: str, deal_id: str, path: str, version: str | None) -> list[tuple[str, Fact]]:
    """The other live statements of one concept: every assertion of the path from another
    source that no analyst has set aside and whose document is not archived. Held
    assertions count: a disagreement is never compared away because it awaits review."""
    cur.execute(
        "SELECT * FROM public.ledger_peer_assertions(%s,%s,%s,%s)",
        (tenant_id, deal_id, path, version),
    )
    out = []
    for aid, payload in cur.fetchall():
        payload = dict(payload if isinstance(payload, dict) else json.loads(payload))
        payload["assertion_id"] = aid
        out.append((aid, _typed_fact(path, payload)))
    return out


def _in_authority(cur, tenant_id: str, concept: Concept, fact: Fact) -> bool:
    """Whether the fact's document is filed in a slot the concept names as a source.

    A disagreement between a source inside the concept's authority and one outside
    it (a valuation restating the facility limit) is noted, not blocking
    (DECISIONS 2026-10-09); two sources inside it still block (D5: stop and ask)."""
    return _rank(cur, tenant_id, concept, fact) < len(concept.authority)


def _rank(cur, tenant_id: str, concept: Concept, fact: Fact) -> int:
    """The fact's place in the concept's authority order, by its document's mapped slot
    (D3: intake slots are user-confirmed). An unmapped document ranks last."""
    if fact.provenance is None:
        return len(concept.authority)
    cur.execute(
        "SELECT * FROM public.ledger_document_slots(%s,%s)",
        (tenant_id, fact.provenance.document_id),
    )
    slots = {r[0] for r in cur.fetchall()}
    ranks = [i for i, slot in enumerate(concept.authority) if slot in slots]
    return min(ranks) if ranks else len(concept.authority)


def _outranks(
    cur,
    tenant_id: str,
    concept: Concept,
    fact: Fact,
    previous: Any,
    superseded_versions: set[str],
) -> bool:
    """Whether an unheld statement becomes the projected value (rule level 1: authority).

    Authority only chooses the value; the disagreement is already in the register. It
    replaces the projected value when there is none, when that value is its own
    source's, a superseded draft's or another period's, or when it ranks higher. On a
    tie a differing value does not replace the projected one: no last writer wins.
    """
    if not isinstance(previous, Fact) or previous.value is None or previous.provenance is None:
        return True
    if _source(previous) == _source(fact):
        return True
    diff = classify(concept, fact, previous, superseded=superseded_versions.__contains__)
    if diff in (DiffClass.SUPERSEDED, DiffClass.TEMPORAL):
        return True
    mine, theirs = _rank(cur, tenant_id, concept, fact), _rank(cur, tenant_id, concept, previous)
    return mine < theirs or (mine == theirs and diff is DiffClass.AGREE)


def authority_choice(cur, tenant_id: str, deal_id: str, path: str, assertion_ids: Iterable[str]) -> str:
    """The assertion authority would project among ``assertion_ids``: the best-ranked
    source, the earliest recorded on a tie. What an analyst was presented with."""
    cur.execute(
        "SELECT * FROM public.ledger_authority_assertions(%s,%s,%s::uuid[])",
        (tenant_id, deal_id, list(assertion_ids)),
    )
    rows = cur.fetchall()
    concept = concept_for(path)
    if not rows:
        raise NotFound("Assertion not found")
    if concept is None:
        return rows[0][0]

    def rank(row: tuple[str, Any]) -> int:
        payload = dict(row[1] if isinstance(row[1], dict) else json.loads(row[1]))
        return _rank(cur, tenant_id, concept, _typed_fact(path, payload))

    return min(rows, key=rank)[0]


def commit_outputs(
    tenant_id: str, deal_id: str, outputs: list[ExtractionOutput], *, legacy: bool = False
) -> DealModel:
    """Commit candidates against an optimistic revision snapshot.

    The first transaction takes a short deal lock to fence the worker, import
    any legacy snapshot and read the projection. The second transaction
    reacquires the row with an expected-revision check BEFORE writing any
    assertions. If another writer won in between, the read-only second
    transaction rolls back and the input is replayed against a fresh snapshot.
    Holding the row lock through writes and refresh makes the final projection
    atomic without leaving assertions behind on a failed compare-and-swap.
    """
    from creditpaper.config import EXTRACTION_CONFIDENCE_THRESHOLD
    from creditpaper.ledger.jobs import fence

    max_retries = 3
    for _attempt in range(max_retries):
        # Transaction 1: a bounded lock for fencing/bootstrap and a consistent
        # snapshot. No document or model work occurs while this lock is held.
        with tenant_cursor(tenant_id) as cur:
            revision = lock_deal(cur, tenant_id, deal_id)
            fence(cur)
            now, run_id = clock(cur), str(uuid4())
            _bootstrap(cur, tenant_id, deal_id, now, run_id)
            changed = revision == 0
            projected = _project(
                cur,
                tenant_id,
                deal_id,
                QueryScope(valid_at=now, known_at=now, include_undated=True),
            )

        # Transaction 2: re-lock and compare BEFORE writes. A concurrent
        # projection change retries cleanly without committing orphan facts.
        with tenant_cursor(tenant_id) as cur:
            cur.execute("SET LOCAL statement_timeout = '60s'")
            if lock_deal(cur, tenant_id, deal_id) != revision:
                continue
            fence(cur)
            cur.execute(
                "SELECT * FROM public.ledger_superseded_version_ids(%s,%s)",
                (tenant_id, deal_id),
            )
            superseded_versions = {r[0] for r in cur.fetchall()}
            for path, incoming in (
                (path, incoming) for output in outputs for path, incoming in output.fields.items()
            ):
                if incoming.value is None or incoming.provenance is None:
                    continue
                candidate = projected.model_copy(deep=True)
                check = apply(candidate, ExtractionOutput({path: incoming}, []))
                if check.rejected:
                    raise Hold("Extraction contains a field that failed model validation")
                fact = resolve_path(candidate, path)
                if not isinstance(fact, Fact):
                    raise Hold("Extraction does not identify a fact")
                version = fact.provenance.document_version_id
                if not legacy and fact.provenance.method != ExtractionMethod.DERIVED:
                    cur.execute(
                        "SELECT public.ledger_current_source_document(%s,%s,%s)",
                        (tenant_id, deal_id, version),
                    )
                    source = cur.fetchone()
                    if not version or not source or str(source[0]) != fact.provenance.document_id:
                        raise Hold(
                            "Source version is missing, archived or superseded; re-extract current evidence"
                        )
                previous = resolve_path(projected, path)
                low_confidence = (
                    fact.confidence is not None and fact.confidence < EXTRACTION_CONFIDENCE_THRESHOLD
                )
                unscored = (
                    fact.confidence is None and fact.provenance.method == ExtractionMethod.LLM_EXTRACTION
                )
                concept = None if fact.provenance.method == ExtractionMethod.DERIVED else concept_for(path)
                # Same source (or no concept): a re-extraction that reads a different value
                # is held for review, as it always was. Another source's statement of the
                # same concept is classified instead (spec 3a), below.
                differs = (
                    isinstance(previous, Fact)
                    and previous.value is not None
                    and previous.value != fact.value
                    and (concept is None or _source(previous) == _source(fact))
                )
                # A conflicting historical interval may not be present in today's projection.
                cur.execute(
                    "SELECT public.ledger_history_conflict(%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
                    (
                        tenant_id,
                        deal_id,
                        path,
                        fact.valid_from,
                        fact.valid_to,
                        canonical(fact.model_dump(mode="json")["value"]),
                        concept is None,
                        version,
                    ),
                )
                differs = differs or cur.fetchone() is not None
                classes: dict[DiffClass, list[str]] = {}
                if concept is not None:
                    for peer_id, peer in _peers(cur, tenant_id, deal_id, path, version):
                        diff = classify(concept, fact, peer, superseded=superseded_versions.__contains__)
                        if diff is DiffClass.CONTRADICTION and _in_authority(
                            cur, tenant_id, concept, fact
                        ) != _in_authority(cur, tenant_id, concept, peer):
                            diff = DiffClass.OUT_OF_AUTHORITY
                        if diff is not DiffClass.AGREE:
                            classes.setdefault(diff, []).append(peer_id)
                contradicted = DiffClass.CONTRADICTION in classes
                held = (
                    low_confidence or unscored or differs or contradicted or fact.status == FactStatus.FLAGGED
                )
                review_reasons: tuple[str, ...] = ()
                if held:
                    fact, review_reasons = _with_review_reasons(
                        fact,
                        low_confidence=low_confidence,
                        unscored=unscored,
                        differs=differs,
                        contradicted=contradicted,
                    )
                disposition = "review" if held else ("legacy" if legacy else "accepted")
                aid, created = _assert(
                    cur,
                    tenant_id,
                    deal_id,
                    path,
                    fact,
                    disposition,
                    now,
                    run_id,
                    "legacy_import" if legacy else None,
                )
                if not created:
                    continue
                changed = True
                if concept is not None:
                    for diff, peer_ids in classes.items():
                        contradictions.record(
                            cur,
                            tenant_id=tenant_id,
                            deal_id=deal_id,
                            concept_path=path,
                            assertion_ids=[aid, *peer_ids],
                            diff_class=diff,
                            material=concept.material,
                            registry_version=REGISTRY_VERSION,
                            now=now,
                        )
                if held:
                    cur.execute(
                        "SELECT public.enqueue_fact_review(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb)",
                        (
                            tenant_id,
                            deal_id,
                            path,
                            fact.provenance.document_id,
                            fact.provenance.page,
                            canonical(fact.model_dump(mode="json")["value"]),
                            fact.confidence or 0,
                            aid,
                            canonical(review_reasons),
                        ),
                    )
                elif concept is None or _outranks(
                    cur, tenant_id, concept, fact, previous, superseded_versions
                ):
                    _select(
                        cur,
                        tenant_id,
                        deal_id,
                        path,
                        aid,
                        FactPeriod(valid_from=fact.valid_from, valid_to=fact.valid_to),
                        now,
                        None,
                        "validated extraction",
                    )
                    apply(
                        projected, ExtractionOutput({path: fact.model_copy(update={"assertion_id": aid})}, [])
                    )
            if not changed:
                projected.model_version = revision
                return projected
            return _refresh(cur, tenant_id, deal_id, now, run_id)

    raise Conflict("Concurrent modification during extraction commit; please retry")


def _review_one(
    cur,
    tenant_id: str,
    deal_id: str,
    assertion_id: str,
    actor_id: str,
    action: str,
    reason: str,
    period: FactPeriod | None,
    now: datetime | None = None,
) -> tuple[str, dict, datetime]:
    """Record one review decision on an assertion (the deal is already locked).

    Returns (field_path, stored fact payload, decision time)."""
    cur.execute(
        "SELECT * FROM public.ledger_assertion_row(%s,%s,%s)",
        (tenant_id, deal_id, assertion_id),
    )
    row = cur.fetchone()
    if not row:
        raise NotFound("Assertion not found")
    if action == "accept" and row[3]:
        cur.execute(
            "SELECT public.ledger_source_is_current(%s,%s,%s)",
            (tenant_id, deal_id, row[3]),
        )
        if not cur.fetchone():
            raise Hold("Source was archived or superseded; review current evidence")
    cur.execute("SELECT public.ledger_review_exists(%s,%s)", (tenant_id, assertion_id))
    if cur.fetchone():
        raise Conflict("Assertion has already been reviewed")
    now = now or clock(cur)
    cur.execute(
        "SELECT public.record_ledger_review(%s,%s,%s,%s,%s,%s,%s)",
        (tenant_id, deal_id, assertion_id, action, actor_id, reason, now),
    )
    payload = row[4] if isinstance(row[4], dict) else json.loads(row[4])
    if action == "accept":
        _select(
            cur,
            tenant_id,
            deal_id,
            row[0],
            assertion_id,
            period or FactPeriod(valid_from=row[1], valid_to=row[2]),
            now,
            actor_id,
            reason,
        )
        contradictions.settle(
            cur,
            tenant_id,
            deal_id,
            assertion_id,
            actor_id=actor_id,
            reason=reason,
            now=now,
            chosen_id=assertion_id,
            chosen_value=payload.get("value"),
        )
    else:
        cur.execute(
            "SELECT public.supersede_assertion_resolutions(%s,%s,%s,%s,%s,%s)",
            (tenant_id, deal_id, assertion_id, now, actor_id, reason),
        )
    cur.execute(
        "SELECT public.update_fact_review_status(%s,%s,%s,%s,%s,%s,%s)",
        (
            tenant_id,
            deal_id,
            assertion_id,
            "approved" if action == "accept" else "rejected",
            actor_id,
            now,
            reason,
        ),
    )
    if action != "accept":
        contradictions.settle(
            cur, tenant_id, deal_id, assertion_id, actor_id=actor_id, reason=reason, now=now
        )
    return row[0], payload, now


def _label(
    cur,
    tenant_id: str,
    deal_id: str,
    path: str,
    decided: dict,
    *,
    action: str,
    final_value: Any,
    assertion_id: str,
    actor_id: str,
    now: datetime,
    reason_code: str | None,
) -> None:
    """Every Fact Sheet decision is a label (spec 3c), on the decision's own cursor."""
    record_label(
        cur,
        tenant_id=tenant_id,
        deal_id=deal_id,
        concept_path=path,
        action=action,
        source="analyst",
        extracted_value=decided.get("value"),
        final_value=final_value,
        assertion_id=assertion_id,
        reason_code=reason_code,
        decided_by=actor_id,
        decided_at=now,
        **label_from_assertion(decided),
    )


def decide(
    cur,
    tenant_id: str,
    deal_id: str,
    assertion_id: str,
    actor_id: str,
    action: str,
    reason: str,
    expected_revision: int | None,
    period: FactPeriod | None = None,
    *,
    reason_code: str | None = None,
) -> DealModel:
    lock_deal(cur, tenant_id, deal_id, expected_revision)
    path, payload, now = _review_one(cur, tenant_id, deal_id, assertion_id, actor_id, action, reason, period)
    _label(
        cur,
        tenant_id,
        deal_id,
        path,
        payload,
        action="accepted" if action == "accept" else "set_aside",
        final_value=payload.get("value") if action == "accept" else None,
        assertion_id=assertion_id,
        actor_id=actor_id,
        now=now,
        reason_code=reason_code,
    )
    return _refresh(cur, tenant_id, deal_id, now, str(uuid4()))


def choose_source(
    cur,
    tenant_id: str,
    deal_id: str,
    set_aside_id: str,
    chosen_id: str,
    actor_id: str,
    reason: str,
    expected_revision: int | None,
    period: FactPeriod | None = None,
    *,
    reason_code: str | None = None,
) -> DealModel:
    """The analyst keeps another source's assertion for the same concept.

    One transaction: the presented assertion is set aside, the chosen one accepted, and
    one `other_source` label records both values. The Fact Sheet control that calls this
    is Plan D; the ledger path and its label are here so the decision is never unlabelled.
    """
    if set_aside_id == chosen_id:
        raise Conflict("Choose a different source to replace this fact")
    lock_deal(cur, tenant_id, deal_id, expected_revision)
    path, presented, now = _review_one(
        cur, tenant_id, deal_id, set_aside_id, actor_id, "reject", reason, None
    )
    chosen_path, chosen, _ = _review_one(
        cur, tenant_id, deal_id, chosen_id, actor_id, "accept", reason, period, now
    )
    if chosen_path != path:
        raise Conflict("The chosen source is for a different fact")
    _label(
        cur,
        tenant_id,
        deal_id,
        path,
        presented,
        action="other_source",
        final_value=chosen.get("value"),
        assertion_id=chosen_id,
        actor_id=actor_id,
        now=now,
        reason_code=reason_code,
    )
    return _refresh(cur, tenant_id, deal_id, now, str(uuid4()))


def _approval_checklist(cur, tenant_id: str, deal_id: str) -> dict[str, Any]:
    cur.execute("SELECT public.get_approval_checklist(%s::uuid, %s::text)", (tenant_id, deal_id))
    row = cur.fetchone()
    checklist = row[0] if row and row[0] else None
    if checklist is None:
        raise NotFound("Deal not found")
    if isinstance(checklist, str):
        checklist = json.loads(checklist)
    if not isinstance(checklist, dict):
        raise Hold("Approval checklist could not be loaded")
    return checklist


def approval_readiness(tenant_id: str, deal_id: str) -> dict[str, Any]:
    """Current disclosure, extraction-blocker, contradiction and ledger-revision snapshot."""
    from creditpaper.deals.services.deal_templates import STANDARD_ID, effective_template
    from creditpaper.deals.services.section_resolver import map_sections
    from creditpaper.deals.store import deal_template_store

    deal = deal_template_store.get_deal(tenant_id, deal_id)
    if deal is None:
        raise NotFound("Deal not found")
    _template_id, _source, snapshot = effective_template(tenant_id, deal)
    selected = sorted(
        {number for number in map_sections(snapshot.get("sections") or []).values() if number is not None}
    )
    with tenant_cursor(tenant_id) as cur:
        checklist = _approval_checklist(cur, tenant_id, deal_id)
        cur.execute(
            "SELECT public.get_approval_guardrail_snapshots(%s::uuid, %s::text, %s::integer[])",
            (tenant_id, deal_id, selected),
        )
        guardrail_row = cur.fetchone()
        guardrail_data = guardrail_row[0] if guardrail_row and guardrail_row[0] else {}
        if isinstance(guardrail_data, str):
            guardrail_data = json.loads(guardrail_data)
        return {
            "revision": int(checklist.get("revision") or 0),
            "outstanding_items": checklist.get("outstanding_items") or [],
            "blocking_documents": checklist.get("blocking_documents") or [],
            "failed_document_jobs": checklist.get("failed_document_jobs") or [],
            "contradictions": contradictions.open_contradictions(cur, tenant_id, deal_id),
            "guardrail_blockers": guardrail_data.get("blocking_items") or [],
            "template_id": _template_id or STANDARD_ID,
        }


def ensure_clear(
    cur,
    tenant_id: str,
    deal_id: str,
    projected: DealModel,
    acknowledged_outstanding_item_ids: list[str] | None = None,
) -> list[ApprovalDisclosure]:
    from creditpaper.deals.utils.ingest import _reconcile

    checklist = _approval_checklist(cur, tenant_id, deal_id)
    if checklist.get("processing_pending"):
        raise Hold("Processing must finish before approval")
    if checklist.get("processing_failed"):
        raise Hold("Extraction Hold: latest processing failed; retry before approval")
    if checklist.get("failed_document_jobs"):
        failed = [
            ApprovalDocumentBlocker.model_validate(item)
            for item in checklist["failed_document_jobs"]
            if isinstance(item, dict)
        ]
        raise ApprovalDocumentsNotReady(failed)
    if checklist.get("pending_review"):
        raise Hold("Extraction Hold: unresolved facts require review")

    blocking_documents = [
        ApprovalDocumentBlocker.model_validate(item)
        for item in checklist.get("blocking_documents", [])
        if isinstance(item, dict)
    ]
    if blocking_documents:
        raise ApprovalDocumentsNotReady(blocking_documents)

    if _reconcile(projected):
        raise Hold("Extraction Hold: reconciliation must be resolved before approval")
    disclosures = [
        ApprovalDisclosure.model_validate(item)
        for item in checklist.get("outstanding_items", [])
        if isinstance(item, dict)
    ]
    expected_ids = sorted(item.id for item in disclosures)
    raw_acknowledged_ids = acknowledged_outstanding_item_ids or []
    if len(raw_acknowledged_ids) != len(set(raw_acknowledged_ids)):
        raise Conflict("Duplicate document acknowledgement")
    acknowledged_ids = sorted(raw_acknowledged_ids)
    if expected_ids != acknowledged_ids:
        if expected_ids:
            raise ApprovalDisclosuresNotAcknowledged(disclosures)
        raise Conflict("The outstanding documents changed; reload the approval checklist")
    return disclosures


def current_recommendation(cur, tenant_id: str, deal_id: str) -> dict[str, Any]:
    """D8: the Section 9 recommendation is a dedicated field only a person writes, signed by a
    named user (`deal_recommendations`, migration 0023; ruled 2026-09-15). Approval rests on the
    latest signed version. Refuses (RecommendationRequired) when there is none; returns the
    reference the approval manifest records.

    Replaces the interim gate (2d9c229) that accepted any human edit to Section 9: a generic
    edit cannot show the recommendation itself was written by a person.
    """
    cur.execute(
        "SELECT * FROM public.current_deal_recommendation(%s,%s)",
        (tenant_id, deal_id),
    )
    row = cur.fetchone()
    if not row:
        raise RecommendationRequired(RECOMMENDATION_REQUIRED_MESSAGE)
    return {"id": str(row[0]), "author_id": str(row[1]), "signed_at": row[2], "body": row[3]}


def _approval_template_identity(paper_data: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    """Resolve the frozen snapshot, or a read-time default for a legacy deal.

    The backing payloads and deal columns come from one tenant-scoped database function;
    this helper only normalizes that captured data and never makes another query.
    """
    from creditpaper.deals.services.deal_templates import STANDARD_ID, firm_snapshot, standard_snapshot

    snapshot = paper_data.get("template_snapshot")
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    if isinstance(snapshot, dict) and isinstance(snapshot.get("sections"), list):
        version_ref = snapshot.get("version_ref") or {}
        template_id = paper_data.get("template_id") or version_ref.get("template_id") or STANDARD_ID
        source = paper_data.get("template_source") or (
            "platform_standard" if version_ref.get("kind") == "platform" else "analyst_choice"
        )
        return snapshot, str(template_id), str(source)

    # Legacy deals without a frozen snapshot use the same firm-default fallback as the
    # paper workspace. Snapshot-bearing deals never consult a mutable template row.
    firm_default = paper_data.get("firm_default")
    if isinstance(firm_default, dict):
        firm_default_snapshot = firm_snapshot(firm_default)
        if firm_default_snapshot["sections"]:
            return firm_default_snapshot, str(firm_default["id"]), "firm_default"

    platform_default = paper_data.get("platform_default")
    if isinstance(platform_default, dict):
        return standard_snapshot(platform_default), STANDARD_ID, "platform_standard"
    raise Hold("No approval template is available for this deal")


def freeze_approval(
    cur,
    tenant_id: str,
    deal_id: str,
    actor_id: str,
    acknowledged_outstanding_item_ids: list[str] | None = None,
    *,
    acknowledged_contradiction_ids: list[str] | None = None,
) -> str:
    revision = lock_deal(cur, tenant_id, deal_id)
    if not revision:
        raise Hold("Import the deal into the ledger before approval")
    now = clock(cur)
    scope = QueryScope(valid_at=now, known_at=now, include_undated=True)
    projected = _project(cur, tenant_id, deal_id, scope)
    projected.model_version = revision
    if acknowledged_outstanding_item_ids is None:
        disclosures = ensure_clear(cur, tenant_id, deal_id, projected)
    else:
        disclosures = ensure_clear(cur, tenant_id, deal_id, projected, acknowledged_outstanding_item_ids)
    open_contradictions = contradictions.open_contradictions(cur, tenant_id, deal_id)
    expected_contradiction_ids = sorted(str(item.get("id")) for item in open_contradictions)
    raw_acknowledged_ids = acknowledged_contradiction_ids or []
    if len(raw_acknowledged_ids) != len(set(raw_acknowledged_ids)):
        raise Conflict("Duplicate contradiction acknowledgement")
    acknowledged_ids = sorted(raw_acknowledged_ids)
    blocking_contradictions = [item for item in open_contradictions if item.get("blocking")]
    if blocking_contradictions or (
        expected_contradiction_ids and expected_contradiction_ids != acknowledged_ids
    ):
        raise ApprovalContradictionsNotAcknowledged(open_contradictions)
    if not expected_contradiction_ids and acknowledged_ids:
        raise Conflict("The contradiction register changed; reload the approval checklist")
    cur.execute(
        "SELECT public.get_approval_paper_data(%s::uuid, %s::text, %s::uuid)",
        (tenant_id, deal_id, actor_id),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        raise NotFound("Deal not found")
    paper_data = row[0]
    if isinstance(paper_data, str):
        paper_data = json.loads(paper_data)
    if not isinstance(paper_data, dict):
        raise Hold("The selected approval template could not be loaded")
    snapshot, approval_template_id, approval_template_source = _approval_template_identity(paper_data)
    profile = paper_data.get("profile") if isinstance(paper_data.get("profile"), dict) else {}
    from creditpaper.deals.services.deal_templates import build_snapshot
    from creditpaper.deals.services.section_resolver import map_sections

    template_sections = snapshot.get("sections") if isinstance(snapshot, dict) else []
    template_sections = template_sections if isinstance(template_sections, list) else []
    has_template_keys = all(
        isinstance(section, dict) and isinstance(section.get("key"), str) for section in template_sections
    )
    if not has_template_keys:
        version_ref = snapshot.get("version_ref") if isinstance(snapshot, dict) else None
        snapshot = build_snapshot(
            str(snapshot.get("name") or "Approved paper"),
            template_sections,
            version_ref if isinstance(version_ref, dict) else {},
        )
        template_sections = snapshot["sections"]
    mapped = map_sections(template_sections)
    section_details = {str(s.get("key")): s for s in template_sections if isinstance(s, dict)}
    selected: dict[int, tuple[str, dict[str, Any]]] = {}
    for key, number in mapped.items():
        if number is not None:
            selected.setdefault(number, (key, section_details.get(key, {})))

    all_sections = [tuple(item) for item in paper_data.get("sections", [])]
    sections_by_number = {int(section[0]): section for section in all_sections}
    blockers: list[PaperSectionBlocker] = []
    for number, (key, detail) in sorted(selected.items()):
        paper_section = sections_by_number.get(number)
        name = str(detail.get("name") or f"Section {number}")
        template_number = str(detail.get("number") or number)
        if paper_section is None:
            reason = "missing"
        elif paper_section[1] != "ready":
            reason = "not_ready"
        elif paper_section[4] != revision:
            reason = "stale"
        else:
            continue
        blockers.append(
            PaperSectionBlocker(
                key=key,
                number=template_number,
                engine_section=number,
                name=name,
                reason=reason,
            )
        )
    if blockers:
        raise PaperSectionsNotReady(blockers)

    sections = [sections_by_number[number] for number in sorted(selected)]
    recommendation = paper_data.get("recommendation")
    if not isinstance(recommendation, dict):
        # Keep the independent D8 signed-recommendation gate even if a malformed
        # procedure result omits the optional presentation payload.
        recommendation = current_recommendation(cur, tenant_id, deal_id)
    else:
        recommendation = {
            "id": str(recommendation["id"]),
            "author_id": str(recommendation["author_id"]),
            "author_name": str(recommendation.get("author_name") or "Former member"),
            "signed_at": recommendation["signed_at"],
            "body": recommendation["body"],
        }
    engine_sections = sorted(selected)
    cur.execute(
        "SELECT public.get_approval_guardrail_snapshots(%s::uuid, %s::text, %s::integer[])",
        (tenant_id, deal_id, engine_sections),
    )
    guardrail_row = cur.fetchone()
    guardrail_manifest = guardrail_row[0] if guardrail_row and guardrail_row[0] else {}
    if isinstance(guardrail_manifest, str):
        guardrail_manifest = json.loads(guardrail_manifest)
    if not isinstance(guardrail_manifest, dict):
        raise Hold("Generation-time guardrail records could not be loaded")
    blocking_guardrails = guardrail_manifest.get("blocking_items") or []
    if blocking_guardrails:
        raise ApprovalGuardrailsNotSignedOff(blocking_guardrails)

    manifest = {
        "schema_version": projected.schema_version,
        "model": projected.model_dump(mode="json"),
        "facts": _history(cur, tenant_id, deal_id, scope),
        "sections": [row[:5] for row in sections],
        "recommendation": recommendation,
        "outstanding_items": [item.model_dump(mode="json") for item in disclosures],
        "open_contradictions": open_contradictions,
        "guardrails": guardrail_manifest.get("sections", []),
        "guardrail_signoffs": guardrail_manifest.get("signoffs", []),
        "constitution_versions": sorted(
            {
                str(item.get("constitution_version"))
                for item in guardrail_manifest.get("sections", [])
                if item.get("constitution_version")
            }
        ),
        "profile": profile,
        "approval": {
            "actor_id": str(actor_id),
            "actor_name": str(paper_data.get("actor_name") or "Former member"),
            "approved_at": now,
            "valid_at": now,
            "ledger_revision": revision,
            "outstanding_items_acknowledged": bool(disclosures),
            "outstanding_item_ids": sorted(item.id for item in disclosures),
            "acknowledged_contradiction_ids": acknowledged_ids,
        },
    }
    from creditpaper.deals.services.final_paper import freeze_paper

    manifest["paper"] = freeze_paper(
        cur,
        tenant_id,
        deal_id,
        sections,
        profile,
        outstanding_items=[item.model_dump(mode="json") for item in disclosures],
        open_contradictions=open_contradictions,
        template_override={
            "id": approval_template_id,
            "source": approval_template_source,
            "snapshot": snapshot,
            "name": paper_data.get("name") or "Approved paper",
            "theme_default": paper_data.get("theme_default"),
        },
    )
    cur.execute(
        "SELECT public.record_paper_approved_event(%s::uuid, %s::text, %s::jsonb)",
        (tenant_id, deal_id, canonical({"approval_actor_id": str(actor_id), "approval_revision": revision})),
    )
    cur.execute(
        "SELECT public.create_ledger_approval(%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
        (tenant_id, deal_id, actor_id, now, now, revision, canonical(manifest), digest(manifest)),
    )
    return str(cur.fetchone()[0])


def read_approval(tenant_id: str, deal_id: str, approval_id: str) -> dict:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            "SELECT * FROM public.ledger_approval_row(%s,%s,%s)",
            (tenant_id, deal_id, approval_id),
        )
        row = cur.fetchone()
        if not row:
            raise NotFound("Approval not found")
        return {"manifest": row[0], "valid_at": row[1], "known_at": row[2], "digest": row[3]}


def review_queue_item(
    tenant_id: str,
    deal_id: str,
    queue_id: str,
    actor_id: str,
    *,
    action: str,
    reason: str,
    expected_revision: int,
    corrected_value: str | None = None,
    reason_code: str | None = None,
    on_decided: Callable[[Any, str, str], None] | None = None,
) -> dict:
    """Decide one review queue item in one transaction.

    `on_decided(cur, status, field_path)` runs on the same cursor after the decision is
    written and before commit, so side records (the deal activity event) share its fate.
    """
    with tenant_cursor(tenant_id) as cur:
        lock_deal(cur, tenant_id, deal_id, expected_revision)
        cur.execute(
            "SELECT * FROM public.lock_fact_review_item(%s,%s,%s)",
            (tenant_id, deal_id, queue_id),
        )
        item = cur.fetchone()
        if not item:
            raise NotFound("Review item not found")
        if item[1] != "pending":
            raise Conflict("Review item has already been decided")
        if not item[0]:
            raise Hold("Legacy review item must be re-extracted with a source version")
        aid = str(item[0])
        if corrected_value is None or action == "reject":
            saved = decide(
                cur,
                tenant_id,
                deal_id,
                aid,
                actor_id,
                action,
                reason,
                expected_revision,
                reason_code=reason_code,
            )
        else:
            cur.execute(
                "SELECT public.ledger_assertion_fact(%s,%s,%s)",
                (tenant_id, deal_id, aid),
            )
            payload = cur.fetchone()[0]
            presented = copy.deepcopy(payload)
            version = payload["provenance"].get("document_version_id")
            if version:
                cur.execute(
                    "SELECT public.ledger_source_is_current(%s,%s,%s)",
                    (tenant_id, deal_id, version),
                )
                if not cur.fetchone():
                    raise Hold("Source was archived or superseded; review current evidence")
            payload["value"] = corrected_value
            payload["provenance"]["method"] = "human_entry"
            payload["status"] = "confirmed"
            payload["confidence"] = 1
            try:
                fact = _typed_fact(item[2], payload)
            except (ValueError, TypeError):
                raise Hold("Corrected value does not match the field type") from None
            now, run_id = clock(cur), str(uuid4())
            new_id, created = _assert(cur, tenant_id, deal_id, item[2], fact, "accepted", now, run_id)
            if not created:
                raise Conflict("This correction has already been recorded")
            for fact_id, decision in ((aid, "reject"), (new_id, "accept")):
                cur.execute(
                    "SELECT public.record_ledger_review(%s,%s,%s,%s,%s,%s,%s)",
                    (tenant_id, deal_id, fact_id, decision, actor_id, reason, now),
                )
            _label(
                cur,
                tenant_id,
                deal_id,
                item[2],
                presented,
                action="corrected",
                final_value=fact.model_dump(mode="json")["value"],
                assertion_id=new_id,
                actor_id=actor_id,
                now=now,
                reason_code=reason_code,
            )
            _select(
                cur,
                tenant_id,
                deal_id,
                item[2],
                new_id,
                FactPeriod(valid_from=fact.valid_from, valid_to=fact.valid_to),
                now,
                actor_id,
                reason,
            )
            contradictions.settle(
                cur,
                tenant_id,
                deal_id,
                aid,
                actor_id=actor_id,
                reason=reason,
                now=now,
                chosen_id=new_id,
                chosen_value=fact.model_dump(mode="json")["value"],
            )
            cur.execute(
                "SELECT public.correct_fact_review(%s,%s,%s,%s::jsonb,%s,%s,%s)",
                (tenant_id, deal_id, queue_id, canonical(corrected_value), actor_id, now, reason),
            )
            saved = _refresh(cur, tenant_id, deal_id, now, run_id)
        status = (
            "corrected"
            if corrected_value is not None and action != "reject"
            else ("approved" if action == "accept" else "rejected")
        )
        if on_decided is not None:
            on_decided(cur, status, item[2])
        return {"status": status, "revision": saved.model_version}


def archive_document(tenant_id: str, deal_id: str, document_id: str, actor_id: str) -> None:
    with tenant_cursor(tenant_id) as cur:
        lock_deal(cur, tenant_id, deal_id)
        now = clock(cur)
        cur.execute(
            "SELECT public.archive_ledger_document(%s,%s,%s,%s,%s)",
            (tenant_id, deal_id, document_id, actor_id, now),
        )
        if not cur.fetchone()[0]:
            raise NotFound("Document not found")
        _refresh(cur, tenant_id, deal_id, now, str(uuid4()))
