"""The deal's slot board: the RFI list resolved for its facility type (contract 2026-09-15 §A).

Rules this module keeps:
  - D3. The matcher only PROPOSES. A slot reaches `confirmed` only through confirm, change
    or assign, one slot per call; there is no bulk confirm. A decision on a multi-file slot
    never silently confirms other files still only proposed there: they move to candidates.
  - D7. Refused classes stay refused: a refused file is never proposed, assigned or read.
  - Nothing is dropped: a file matching no slot is Unassigned and hand-assignable.
  - Empty non-optional slots are the disclosed outstanding items; they never block anything.
  - Every decision is written to the deal's audit trail (Flow E: confirmations are stored
    even before anything learns from them).
  - The board never writes the deal model. Withdrawal of a cited document is refused
    (fact invalidation is a later piece).

Stored slot status vs. what the board shows: a slot whose extraction was started is stored
`processing`; the board shows `confirmed` once every file's extraction reports ready and
`failed` (with the reason) if any reports failed or invalid.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from threading import Lock
from typing import Any

from creditpaper.deals.services import slot_matcher
from creditpaper.deals.services.fact_walk import citing_paths
from creditpaper.deals.services.slot_taxonomy import (
    DEAL_TYPE_REQUIRED_MESSAGE,
    RefusedClass,
    SlotType,
    is_facility_type,
    refused_class_from_row,
    required_slot_keys,
    slot_type_from_row,
    slots_for,
)
from creditpaper.deals.store import slot_store

log = logging.getLogger(__name__)

# Only derived findings are retained, never document text. The database validates
# both tenant visibility and the current extraction fingerprint on every read.
_BUNDLE_CACHE_LIMIT = 2048
_bundle_cache: OrderedDict[tuple[str, str], tuple[str, dict[str, Any] | None]] = OrderedDict()
_bundle_cache_lock = Lock()

DECIDED = ("confirmed", "processing", "failed")

Row = dict[str, Any]
CHOSEN_REASON = "Chosen by the analyst."


class DealNotFound(Exception):
    """404 DEAL_NOT_FOUND. Another tenant's deal is indistinguishable from none."""


class DealTypeRequired(Exception):
    """409 DEAL_TYPE_REQUIRED."""

    message = DEAL_TYPE_REQUIRED_MESSAGE


class SlotNotFound(Exception):
    """404 SLOT_NOT_FOUND."""


class DocumentNotFound(Exception):
    """404 DOCUMENT_NOT_FOUND."""


class SlotConflict(Exception):
    """A 409 with its own code and message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ExtractionJob:
    document_id: str
    version_id: str
    extractor: str
    job_id: str | None = None


@dataclass
class Ctx:
    tenant_id: str
    deal_id: str
    deal_type: str
    slots: list[SlotType]
    refused: list[RefusedClass]
    rows: dict[str, dict[str, Any]]  # slot_key -> stored slot row
    files: list[dict[str, Any]]
    documents: dict[str, dict[str, Any]]  # document_id -> document with versions
    match_inputs: dict[str, tuple[str | None, str]] | None = None
    content_hashes: dict[str, str | None] = field(default_factory=dict)

    def slot_by_id(self, slot_id: str) -> tuple[SlotType, dict[str, Any]]:
        for slot in self.slots:
            row = self.rows.get(slot.key)
            if row and row["id"] == slot_id:
                return slot, row
        raise SlotNotFound(slot_id)

    def assigned(self, slot_id: str) -> list[dict[str, Any]]:
        return [
            f
            for f in self.files
            if f["slot_id"] == slot_id and f["role"] == "assigned" and f["document_id"] in self.documents
        ]

    def candidates(self, slot_id: str) -> list[dict[str, Any]]:
        return [
            f
            for f in self.files
            if f["slot_id"] == slot_id and f["role"] == "candidate" and f["document_id"] in self.documents
        ]


# --- context -----------------------------------------------------------------------


def _slot_types() -> list[SlotType]:
    return [slot_type_from_row(r) for r in slot_store.slot_types()]


def _refused() -> list[RefusedClass]:
    return [refused_class_from_row(r) for r in slot_store.refused_classes()]


def _ctx_from_raw(tenant_id: str, deal_id: str, raw: dict[str, Any]) -> Ctx:
    """Build board context from a stored-function payload."""
    deal = raw.get("deal") or {}
    if not deal:
        raise DealNotFound(deal_id)
    deal_type = deal.get("deal_type")
    if not is_facility_type(deal_type):
        raise DealTypeRequired(deal_id)
    assert isinstance(deal_type, str)

    slots = slots_for(deal_type, [slot_type_from_row(row) for row in raw.get("slot_types") or []])
    rows = {row["slot_key"]: {**row, "id": str(row["id"])} for row in raw.get("slots") or []}
    files = [
        {
            **file,
            "slot_id": str(file["slot_id"]),
            "document_id": str(file["document_id"]),
            "version_id": str(file["version_id"]) if file.get("version_id") is not None else None,
        }
        for file in raw.get("files") or []
    ]
    documents = {}
    for document in raw.get("documents") or []:
        document_id = str(document["document_id"])
        versions = [
            {
                **version,
                "version_id": str(version["version_id"]),
            }
            for version in document.get("versions") or []
        ]
        documents[document_id] = {**document, "document_id": document_id, "versions": versions}

    return Ctx(
        tenant_id=tenant_id,
        deal_id=deal_id,
        deal_type=deal_type,
        slots=slots,
        refused=[refused_class_from_row(row) for row in raw.get("refused") or []],
        rows=rows,
        files=files,
        documents=documents,
    )


def board_from_raw(tenant_id: str, deal_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Render an already-loaded stored-function board payload."""
    return _board(_ctx_from_raw(tenant_id, deal_id, raw), include_checks=False)


def _load(tenant_id: str, deal_id: str) -> Ctx:
    """Load the board snapshot in one database call instead of serial per-table reads."""
    raw = slot_store.call_slot_board(tenant_id, deal_id)
    return _ctx_from_raw(tenant_id, deal_id, raw)


def _names(doc: dict[str, Any]) -> list[str]:
    names = [doc.get("display_name") or ""]
    names += [v.get("original_filename") or "" for v in doc.get("versions") or []]
    return [n for n in dict.fromkeys(names) if n]


def _current_version(doc: dict[str, Any]) -> dict[str, Any]:
    versions = doc.get("versions") or []
    current = [v for v in versions if not v.get("superseded_at")]
    ordered = sorted(current or versions, key=lambda v: v.get("version_number") or 0, reverse=True)
    return ordered[0] if ordered else {}


def _version(doc: dict[str, Any], version_id: str | None) -> dict[str, Any]:
    for v in doc.get("versions") or []:
        if v.get("version_id") == version_id:
            return dict(v)
    return _current_version(doc)


def _display_name(ctx: Ctx, document_id: str) -> str | None:
    """The name Activity shows for a document: its display name, else its current filename."""
    doc = ctx.documents.get(document_id)
    if doc is None:
        return None
    return doc.get("display_name") or _current_version(doc).get("original_filename") or None


def _activity_names(ctx: Ctx, document_ids: list[str]) -> dict[str, Any]:
    """`document_name` (one file) or `document_names` (several) for Activity labels."""
    names = [n for n in (_display_name(ctx, d) for d in document_ids) if n]
    if len(names) == 1:
        return {"document_name": names[0]}
    return {"document_names": names} if names else {}


def _refusal(ctx: Ctx, doc: dict[str, Any]) -> RefusedClass | None:
    return slot_matcher.refused_match(_names(doc), ctx.refused)


# --- board -------------------------------------------------------------------------


FIRST_PAGES = 3


def _first_pages(pages: dict[int, str] | None) -> dict[int, str]:
    """The opening pages, which is where a document says what it is.

    Bounded deliberately: the signals read a title block and a signature block,
    and handing them a 400-page bundle would shingle the whole thing to decide
    one label.
    """
    if not pages:
        return {}
    return {n: pages[n] for n in sorted(pages)[:FIRST_PAGES]}


def _labelled_corpus(tenant_id: str) -> list[tuple[str, str]]:
    """This tenant's own confirmations, for the neighbour signal.

    Never fatal. A firm with no history yet simply has no corpus, and a corpus
    that cannot be read is a worse proposal, not a failed intake.
    """
    from creditpaper.ingest import intake_store

    try:
        return intake_store.labelled_corpus(tenant_id)
    except Exception:  # noqa: BLE001 - intake proceeds without the corpus
        log.exception("could not read the labelled corpus; proposing without it")
        return []


def _pages_by_version(tenant_id: str, version_ids: list[str]) -> dict[str, dict[int, str]]:
    """{version_id: {page: text}} for many versions in ONE query.

    Both the bundle check and the content signals need page text for every
    assigned file. The board resolves ~23 slots, so reading per slot would be 23
    round trips for one column; this is read once and shared.
    """
    if not version_ids:
        return {}
    import json

    rows = slot_store.bundle_inputs(tenant_id, version_ids, {})

    out: dict[str, dict[int, str]] = {}
    for version_id, _fingerprint, raw in rows:
        payload = json.loads(raw) if isinstance(raw, str) else (raw or [])
        out[str(version_id)] = {int(p["page"]): str(p.get("text") or "") for p in payload if p.get("page")}
    return out


def _bundle_checks(tenant_id: str, version_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not version_ids:
        return {}
    with _bundle_cache_lock:
        snapshot = {
            v: _bundle_cache[(tenant_id, v)] for v in set(version_ids) if (tenant_id, v) in _bundle_cache
        }
    rows = slot_store.bundle_inputs(tenant_id, version_ids, {v: item[0] for v, item in snapshot.items()})
    out = {}
    for version, fingerprint, payload in rows:
        if payload is None:
            # Snapshot remains valid even if another request evicts the entry.
            old_fingerprint, check = snapshot[version]
            if old_fingerprint != fingerprint:
                raise ValueError("Bundle fingerprint mismatch")
        else:
            pages = {int(p["page"]): str(p.get("text") or "") for p in payload if p.get("page")}
            check = _checks_from_pages({version: pages}).get(version)
        with _bundle_cache_lock:
            _bundle_cache[(tenant_id, version)] = (fingerprint, check)
            _bundle_cache.move_to_end((tenant_id, version))
            while len(_bundle_cache) > _BUNDLE_CACHE_LIMIT:
                _bundle_cache.popitem(last=False)
        if check is not None:
            out[version] = dict(check)
    return out


def _checks_from_pages(pages_by_version: dict[str, dict[int, str]]) -> dict[str, dict[str, Any]]:
    """The "Single document" check for every assigned version, in one query.

    A scanned pack holding a valuation AND a QS report behind one cover reads as
    one document, and whichever slot wins takes the whole thing — the other
    document is lost with nothing to show for it. `detect_boundaries` has never
    run in production; this is its first caller.

    A disclosure, never a refusal. A genuine appendix can restart its numbering,
    and refusing would make the analyst fight the intake screen. What must not
    happen is the second document disappearing in silence.
    """
    from creditpaper.ingest.bundles import detect_boundaries

    out: dict[str, dict[str, Any]] = {}
    for version_id, pages in pages_by_version.items():
        if len(pages) < 2:
            continue
        finding = detect_boundaries(pages)
        if not finding.is_bundle:
            out[version_id] = {
                "label": "Single document",
                "passed": True,
                "detail": "One document.",
            }
        elif finding.confirmed_by_page_numbering and finding.boundaries:
            out[version_id] = {
                "label": "Single document",
                "passed": False,
                "detail": (
                    f"More than one document: page numbering restarts at page {finding.boundaries[0]}."
                ),
            }
        else:
            # Without page numbering, boundary detection scores F1 0.40 and
            # `bundles.py` deliberately refuses to name a split point. Nor does
            # this: `boundaries` is empty in that regime.
            out[version_id] = {
                "label": "Single document",
                "passed": False,
                "detail": "This may hold more than one document; a later page looks like a cover page.",
            }
    return out


def _iso(value: object) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)


def _slot_file(doc: dict[str, Any], version_id: str | None = None) -> dict[str, Any]:
    v = _version(doc, version_id)
    return {
        "document_id": doc["document_id"],
        "version_id": v.get("version_id"),
        "filename": v.get("original_filename") or doc.get("display_name"),
        "path": doc.get("display_name") or v.get("original_filename"),
        "size_bytes": int(v.get("size_bytes") or 0),
        "uploaded_at": _iso(v.get("uploaded_at")),
    }


def display_status(ctx: Ctx, slot: SlotType, row: dict[str, Any]) -> tuple[str, str | None]:
    """(status the board shows, error)."""
    stored = row["status"]
    assigned = ctx.assigned(row["id"])
    if stored in ("proposed", "confirmed", "processing") and not assigned:
        return "empty", None
    if stored != "processing":
        return stored, row.get("error") if stored == "failed" else None
    statuses = []
    for f in assigned:
        v = _version(ctx.documents[f["document_id"]], f["version_id"])
        statuses.append((v.get("extraction_status"), v.get("extraction_error")))
    for status, error in statuses:
        if status in ("failed", "invalid"):
            return "failed", error or "Extraction failed."
    if statuses and all(s == "ready" for s, _ in statuses):
        return "confirmed", None
    return "processing", None


def _board(ctx: Ctx, *, include_checks: bool = True) -> dict[str, Any]:
    slots_out = []
    assigned_docs: set[str] = set()
    required = confirmed_required = outstanding = optional = 0
    # One query for every assigned version, not one per slot: the board resolves
    # ~23 slots and a per-slot read would be 23 round trips for one column.
    checks_by_version: dict[str, dict[str, Any]] = {}
    if include_checks:
        checks_started = time.perf_counter()
        checks_by_version = _bundle_checks(
            ctx.tenant_id,
            [str(f["version_id"]) for f in ctx.files if f["role"] == "assigned" and f.get("version_id")],
        )
        logging.getLogger("uvicorn.error").info(
            "slot_board_bundle_checks_ms=%.2f versions=%d",
            (time.perf_counter() - checks_started) * 1000,
            len(checks_by_version),
        )
    for slot in ctx.slots:
        row = ctx.rows[slot.key]
        status, error = display_status(ctx, slot, row)
        assigned = ctx.assigned(row["id"]) if status != "empty" else []
        assigned_docs.update(f["document_id"] for f in ctx.assigned(row["id"]))
        is_optional = slot.optional(ctx.deal_type)
        if is_optional:
            optional += 1
        else:
            required += 1
            if status == "confirmed":
                confirmed_required += 1
            else:
                outstanding += 1
        slots_out.append(
            {
                "id": row["id"],
                "key": slot.key,
                "name": slot.name,
                "description": slot.description,
                "category": slot.category,
                "optional": is_optional,
                "multi": slot.multi,
                "status": status,
                "confidence": row.get("confidence") if status == "proposed" else None,
                "match_reason": row.get("match_reason") if status not in ("empty", "withdrawn") else None,
                "documents": [_slot_file(ctx.documents[f["document_id"]], f["version_id"]) for f in assigned],
                "candidates": [
                    _slot_file(ctx.documents[f["document_id"]], f["version_id"])
                    for f in ctx.candidates(row["id"])
                    if f["document_id"] not in {a["document_id"] for a in assigned}
                ],
                "checks": [
                    check
                    for check in (checks_by_version.get(str(f.get("version_id"))) for f in assigned)
                    if check is not None
                ],
                "error": error,
            }
        )

    unassigned, refused = [], []
    for doc in ctx.documents.values():
        refusal = _refusal(ctx, doc)
        if refusal is not None:
            refused.append({**_slot_file(doc), "reason": refusal.reason})
        elif doc["document_id"] not in assigned_docs:
            unassigned.append(_slot_file(doc))

    return {
        "deal_id": ctx.deal_id,
        "deal_type": ctx.deal_type,
        "slots": slots_out,
        "unassigned": unassigned,
        "refused": refused,
        "summary": {
            "required": required,
            "confirmed_required": confirmed_required,
            "outstanding": outstanding,
            "optional": optional,
        },
    }


def get_board(tenant_id: str, deal_id: str) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = _load(tenant_id, deal_id)
    loaded = time.perf_counter()
    result = _board(ctx)
    finished = time.perf_counter()
    logging.getLogger("uvicorn.error").info(
        "slot_board_phases_ms load=%.2f board=%.2f total=%.2f",
        (loaded - started) * 1000,
        (finished - loaded) * 1000,
        (finished - started) * 1000,
    )
    return result


# --- counts for the deal list ------------------------------------------------------


def document_counts(tenant_id: str, deals: list[tuple[str, str | None]]) -> dict[str, tuple[int, int]]:
    """{deal_id: (document_required, document_count)} for deals with a facility type.

    Same rules as the board summary (`required`, `confirmed_required`), without creating
    slot rows.
    """
    typed = [(d, t) for d, t in deals if is_facility_type(t)]
    if not typed:
        return {}
    types = _slot_types()
    decided = slot_store.confirmed_slot_keys_by_deal(tenant_id, [d for d, _ in typed])
    out = {}
    for deal_id, deal_type in typed:
        assert isinstance(deal_type, str)
        required = required_slot_keys(deal_type, types)
        out[deal_id] = (len(required), len(required & decided.get(deal_id, set())))
    return out


# --- propose -----------------------------------------------------------------------


def propose(
    tenant_id: str, deal_id: str, actor_id: str | None, *, return_board: bool = True
) -> dict[str, Any]:
    ctx = _load(tenant_id, deal_id)
    decided_docs = {f["document_id"] for f in ctx.files}
    table = slot_matcher.keyword_table()
    slot_by_key = {s.key: s for s in ctx.slots}

    # Content evidence, read once for the whole folder. It only ever speaks
    # where the filename is silent (`Scan_0042.pdf`), and the labelled corpus is
    # this tenant's own confirmations — their naming, learned from their work.
    undecided = [
        str(_current_version(d).get("version_id"))
        for d in ctx.documents.values()
        if d["document_id"] not in decided_docs and _current_version(d).get("version_id")
    ]
    pages_by_version = slot_store.match_pages(tenant_id, deal_id, undecided)
    labelled = _labelled_corpus(tenant_id)

    by_slot: dict[str, list[tuple[dict[str, Any], slot_matcher.FileMatch]]] = {}
    add_files: list[dict[str, Any]] = []
    unmatched = refused = 0
    for doc in ctx.documents.values():
        if doc["document_id"] in decided_docs:
            continue
        if _refusal(ctx, doc) is not None:
            refused += 1
            continue
        current_version_id = str(_current_version(doc).get("version_id") or "")
        match = slot_matcher.match_file(
            _names(doc),
            doc.get("document_kind"),
            ctx.slots,
            table,
            first_pages=_first_pages(pages_by_version.get(current_version_id)),
            labelled=labelled,
        )
        if match.slot_key is None:
            unmatched += 1
            continue
        by_slot.setdefault(match.slot_key, []).append((doc, match))
        version_id = _current_version(doc).get("version_id")
        for key, conf, reason in match.candidates:
            add_files.append(
                {
                    "slot_id": ctx.rows[key]["id"],
                    "document_id": doc["document_id"],
                    "version_id": version_id,
                    "role": "candidate",
                    "confidence": conf,
                    "match_reason": reason,
                }
            )

    # Only documents admitted by matching/refusal checks need supersession evidence.
    evidence_ids = []
    for key, matches in by_slot.items():
        slot, row = slot_by_key[key], ctx.rows[key]
        status, _ = display_status(ctx, slot, row)
        if len(matches) >= 2 and status not in DECIDED and not (status == "proposed" and not slot.multi):
            evidence_ids.extend(
                str(_current_version(d)["version_id"])
                for d, _ in matches
                if _current_version(d).get("version_id")
            )
    ctx.match_inputs = slot_store.match_inputs(tenant_id, deal_id, evidence_ids)
    slot_updates: list[dict[str, Any]] = []
    proposed = 0
    for key, matches in by_slot.items():
        slot, row = slot_by_key[key], ctx.rows[key]
        status, _ = display_status(ctx, slot, row)
        ranked = sorted(
            matches,
            key=lambda m: (
                slot_matcher.canonical_score(_current_version(m[0]).get("original_filename") or ""),
                m[1].confidence or 0,
                len(m[0].get("display_name") or ""),
            ),
            reverse=True,
        )

        def _file(
            doc: dict[str, Any], match: slot_matcher.FileMatch, role: str, slot_id: str = row["id"]
        ) -> dict[str, Any]:
            return {
                "slot_id": slot_id,
                "document_id": doc["document_id"],
                "version_id": _current_version(doc).get("version_id"),
                "role": role,
                "confidence": match.confidence,
                "match_reason": match.reason,
            }

        if status in DECIDED or (status == "proposed" and not slot.multi):
            # Never overwrite a decision, and never displace a standing proposal.
            add_files += [_file(d, m, "candidate") for d, m in ranked]
            continue
        # Superseded versions drop out of contention before the choice is made,
        # but stay on the board as candidates — nothing is silently dropped.
        current, superseded_ids = _supersede(ctx, ranked)
        demoted = [(d, m) for d, m in ranked if d["document_id"] in set(superseded_ids)]
        if slot.multi:
            chosen, others = current, demoted
        else:
            chosen, others = current[:1], current[1:] + demoted
        add_files += [_file(d, m, "assigned") for d, m in chosen]
        add_files += [_file(d, m, "candidate") for d, m in others]
        proposed += len(chosen)

        confidences = [m.confidence or 0 for _, m in chosen]
        if status == "proposed" and row.get("confidence") is not None:
            confidences.append(row["confidence"])
        confidence = min(confidences)
        reason = chosen[0][1].reason or ""
        if others:
            confidence = max(0.3, confidence - 0.15)
            count = len(others)
            reason += f" {count} other file{'s' if count > 1 else ''} also matched; listed under Change."
        if superseded_ids:
            count = len(superseded_ids)
            reason += f" {count} superseded version{'s' if count > 1 else ''} demoted; listed under Change."
        if slot.multi and len(chosen) > 1:
            reason = f"{len(chosen)} files matched. {reason}"
        slot_updates.append(
            {
                "id": row["id"],
                "status": "proposed",
                "confidence": round(confidence, 2),
                "match_reason": reason.strip(),
                "error": None,
            }
        )

    if add_files or slot_updates:
        slot_store.write_changes(
            tenant_id=tenant_id,
            deal_id=deal_id,
            actor_id=actor_id,
            slot_updates=slot_updates,
            add_files=add_files,
            audit=[
                {
                    "action": "document_slots.proposed",
                    "entity_type": "deal",
                    "entity_id": deal_id,
                    "metadata": {
                        "proposed_files": proposed,
                        "slots": sorted(by_slot),
                        "unmatched_files": unmatched,
                        "refused_files": refused,
                    },
                }
            ],
        )
    if proposed:
        _start_speculation(tenant_id, deal_id)
    if not return_board:
        return {"proposed": proposed, "unmatched": unmatched, "refused": refused}
    return get_board(tenant_id, deal_id)


def _start_speculation(tenant_id: str, deal_id: str) -> None:
    """Read the proposed documents now, so confirming is only a release.

    ONE job for the whole deal. The fan-out happens inside it, because
    `ledger/worker.py` gives a tenant one job per pass so that a busy tenant
    cannot starve the others — a fairness rule ingestion speed should not buy
    its way out of. The deal is the resource, so re-proposing coalesces onto
    the pending job rather than queueing a second read.

    Never fatal: a proposal the analyst can act on is worth more than the
    speculative read behind it, and confirmation still extracts if nothing was
    held.
    """
    from creditpaper.ledger import jobs

    try:
        job_id = jobs.enqueue(tenant_id, deal_id, "ingest_speculate", deal_id, {})
        jobs.dispatch(tenant_id, job_id)
    except Exception:  # noqa: BLE001 - the board stands either way
        log.exception("could not start the speculative read for this deal")


# --- decisions ---------------------------------------------------------------------


def _content_hash(ctx: Ctx, version_id: str | None) -> str | None:
    """The stored content hash for one version, which keys everything in ingest/."""
    if not version_id:
        return None
    if ctx.match_inputs is not None:
        return ctx.match_inputs.get(str(version_id), (None, ""))[0]
    if str(version_id) in ctx.content_hashes:
        return ctx.content_hashes[str(version_id)]
    from creditpaper.db import tenant_cursor, tenant_uuid

    with tenant_cursor(ctx.tenant_id) as cur:
        cur.execute(
            "SELECT content_sha256 FROM deal_document_versions WHERE id = %s AND tenant_id = %s",
            (version_id, tenant_uuid(ctx.tenant_id)),
        )
        row = cur.fetchone()
    value = str(row[0]) if row and row[0] else None
    ctx.content_hashes[str(version_id)] = value
    return value


def _held_documents(ctx: Ctx, files: list[dict[str, Any]]) -> set[str]:
    """Document ids that already have a speculative read waiting.

    Whichever slot it was read under. If it matches, `_release_held` commits it;
    if it does not, `release._requeue` discards it and queues the right
    extractor itself. Either way this decision must not ALSO queue one, or the
    document is read twice — which is the whole thing being fixed.
    """
    from creditpaper.ingest import intake_store

    out: set[str] = set()
    for f in files:
        content_hash = _content_hash(ctx, f.get("version_id"))
        if content_hash is None:
            continue
        try:
            if intake_store.get_held_facts(ctx.tenant_id, ctx.deal_id, content_hash) is not None:
                out.add(f["document_id"])
        except Exception:  # noqa: BLE001 - fall back to extracting, never to losing the fact
            log.exception("could not read the hold; treating the document as unread")
    return out


def _release_held(ctx: Ctx, slot: SlotType, files: list[dict[str, Any]], actor_id: str | None) -> None:
    """A confirmation releases whatever was read speculatively for this document.

    D3 as amended: extraction may start on the PROPOSED slot, and this is the
    moment its result becomes citable — or is discarded, when the slot the
    analyst confirmed is not the one the facts were read under.

    A failure here must never cost the user their decision. The decision is
    already written; the release is a consequence of it. An unreleased hold is
    recoverable, a lost confirmation is not.
    """
    from creditpaper.ingest import release

    for f in files:
        content_hash = _content_hash(ctx, f.get("version_id"))
        if content_hash is None:
            continue
        try:
            release.release_or_discard(
                ctx.tenant_id, ctx.deal_id, content_hash, slot.key, str(actor_id or "")
            )
        except Exception:  # noqa: BLE001 - the decision stands either way
            log.exception("release failed for slot %s; the hold is kept", slot.key)


def _full_text(ctx: Ctx, version_id: str | None) -> str:
    """The parsed text of one version, as the processing pipeline stored it."""
    if not version_id:
        return ""
    if ctx.match_inputs is not None:
        return ctx.match_inputs.get(str(version_id), (None, ""))[1]
    from creditpaper.db import tenant_cursor, tenant_uuid

    with tenant_cursor(ctx.tenant_id) as cur:
        cur.execute(
            "SELECT full_text FROM deal_document_extractions WHERE version_id = %s AND tenant_id = %s",
            (version_id, tenant_uuid(ctx.tenant_id)),
        )
        row = cur.fetchone()
    return str(row[0] or "") if row else ""


def _supersede(
    ctx: Ctx, ranked: list[tuple[dict[str, Any], slot_matcher.FileMatch]]
) -> tuple[list[tuple[dict[str, Any], slot_matcher.FileMatch]], list[str]]:
    """Demote documents a newer member of the same family replaces.

    `_canonical_score` reads the filename; this reads the text. A file named
    `Terms Sheet FINAL.pdf` whose first page says "DRAFT — not for execution" is
    exactly what a filename cannot catch, and exactly what poisons every figure
    downstream with correct-looking provenance.

    `cluster_families` only marks supersession when the operative copy is
    EXECUTED, so a family of two drafts demotes nothing and an ambiguous one
    (both markers present) goes to a person instead. That conservatism is the
    point and is not second-guessed here.

    Returns the surviving matches and the demoted document ids. Nothing is
    dropped: the caller re-offers the demoted files as candidates.
    """
    from creditpaper.ingest.families import cluster_families

    if len(ranked) < 2:
        return ranked, []

    by_hash: dict[str, str] = {}
    docs: list[tuple[str, str]] = []
    for doc, _ in ranked:
        version_id = _current_version(doc).get("version_id")
        content_hash = _content_hash(ctx, version_id)
        if content_hash is None or content_hash in by_hash:
            continue
        by_hash[content_hash] = doc["document_id"]
        docs.append((content_hash, _full_text(ctx, version_id)))

    superseded = {
        by_hash[m.content_hash]
        for family in cluster_families(docs)
        for m in family
        if m.superseded_by is not None
    }
    if not superseded:
        return ranked, []

    kept = [(d, m) for d, m in ranked if d["document_id"] not in superseded]
    # Never demote everything: if the family logic would empty the slot, the
    # ranking stands and nothing is claimed about versions.
    return (kept or ranked), (sorted(superseded) if kept else [])


def _record_confirmation(ctx: Ctx, slot: SlotType, files: list[dict[str, Any]], actor_id: str | None) -> None:
    """Store the analyst's decision as a labelled example.

    Flow E makes this an MVP storage requirement rather than a Phase 2 feature:
    a tenant who repeatedly files what they call a Facility Offer Letter into
    the ITS slot has taught their own router, and without this the lesson is
    discarded every time. A correction is the most valuable label of all — it is
    where the matcher was wrong.

    Content hash and slot key only: no filename, no document text. The corpus is
    tenant data and travels no further than it has to.
    """
    from creditpaper.ingest import intake_store

    for f in files:
        content_hash = _content_hash(ctx, f.get("version_id"))
        if content_hash is None:
            continue
        try:
            intake_store.confirm_slot(ctx.tenant_id, ctx.deal_id, content_hash, slot.key, str(actor_id or ""))
        except Exception:  # noqa: BLE001 - a lost label must never cost a decision
            log.exception("could not record the intake label for slot %s", slot.key)


def _require_document(ctx: Ctx, document_id: str) -> dict[str, Any]:
    doc = ctx.documents.get(document_id)
    if doc is None:
        raise DocumentNotFound(document_id)
    refusal = _refusal(ctx, doc)
    if refusal is not None:
        raise SlotConflict("DOCUMENT_REFUSED", refusal.reason)
    return doc


def _jobs(slot: SlotType, files: list[dict[str, Any]]) -> list[ExtractionJob]:
    if not slot.extractor:
        return []
    return [ExtractionJob(f["document_id"], f["version_id"], slot.extractor) for f in files]


def _moves_from_other_slots(ctx: Ctx, slot_id: str, document_id: str) -> tuple[list[Row], list[Row]]:
    """Take a document out of other slots where it is only proposed. A file confirmed in
    another slot is a conflict: the user unassigns it there first."""
    removals: list[Row] = []
    updates: list[Row] = []
    for other in ctx.slots:
        row = ctx.rows[other.key]
        if row["id"] == slot_id:
            continue
        assigned = ctx.assigned(row["id"])
        if not any(f["document_id"] == document_id for f in assigned):
            continue
        status, _ = display_status(ctx, other, row)
        if status in DECIDED:
            raise SlotConflict(
                "DOCUMENT_ALREADY_CONFIRMED",
                f"This file is confirmed in {other.name}. Unassign it there before using it here.",
            )
        removals.append({"slot_id": row["id"], "document_id": document_id, "role": "assigned"})
        if len(assigned) == 1:
            updates.append({"id": row["id"], "status": "empty", "confidence": None, "match_reason": None})
    return removals, updates


def _decide(
    ctx: Ctx,
    slot: SlotType,
    row: dict[str, Any],
    *,
    keep: list[dict[str, Any]],
    new_doc: dict[str, Any] | None,
    demote: list[dict[str, Any]],
    action: str,
    actor_id: str | None,
    extra_metadata: dict[str, Any],
    match_reason: str | None,
    confidence: float | None,
    extra_removals: list[Row] | None = None,
    extra_updates: list[Row] | None = None,
    durable_confirmation: bool = False,
) -> list[ExtractionJob]:
    """Write a decision: `keep` stay assigned, `new_doc` joins them, `demote` become candidates."""
    removals = list(extra_removals or [])
    adds: list[dict[str, Any]] = []
    for f in demote:
        removals.append({"slot_id": row["id"], "document_id": f["document_id"], "role": "assigned"})
        adds.append({**f, "role": "candidate", "slot_id": row["id"]})
    new_files: list[dict[str, Any]] = []
    if new_doc is not None:
        version_id = _current_version(new_doc).get("version_id")
        removals.append({"slot_id": row["id"], "document_id": new_doc["document_id"], "role": "candidate"})
        new_file = {
            "slot_id": row["id"],
            "document_id": new_doc["document_id"],
            "version_id": version_id,
            "role": "assigned",
            "confidence": None,
            "match_reason": CHOSEN_REASON,
        }
        adds.append(new_file)
        new_files.append(new_file)

    decided_files = keep + new_files
    # Read-only, and it has to happen before write_changes: that call decides
    # `processing` vs `confirmed` from whether a job is needed, so it must know
    # a hold exists. Actually RELEASING here would be wrong in the other
    # direction — facts would reach the model for a confirmation that then
    # failed to write — so the mutation stays below, after the decision is
    # durable.
    held = _held_documents(ctx, decided_files)
    candidates = new_files if action != "document_slot.confirmed" else keep
    to_extract = [f for f in candidates if f["document_id"] not in held]
    jobs = _jobs(slot, to_extract)
    status = "processing" if jobs else "confirmed"
    durable_args = {}
    if durable_confirmation:
        durable_args = {
            "confirmation": {
                "slot_id": row["id"],
                "files": [{"document_id": f["document_id"], "version_id": f["version_id"]} for f in keep],
            },
            "extraction_jobs": [
                {"document_id": j.document_id, "version_id": j.version_id, "extractor": j.extractor}
                for j in jobs
            ],
        }
    job_ids = slot_store.write_changes(
        tenant_id=ctx.tenant_id,
        deal_id=ctx.deal_id,
        actor_id=actor_id,
        slot_updates=[
            *(extra_updates or []),
            {
                "id": row["id"],
                "status": status,
                "confidence": confidence,
                "match_reason": match_reason,
                "error": None,
            },
        ],
        add_files=adds,
        remove_files=removals,
        reset_versions=[j.version_id for j in jobs],
        audit=[
            {
                "action": action,
                "entity_type": "document_slot",
                "entity_id": row["id"],
                "metadata": {
                    "slot_key": slot.key,
                    "document_ids": [f["document_id"] for f in keep + new_files],
                    "demoted_document_ids": [f["document_id"] for f in demote],
                    **extra_metadata,
                    "slot_name": slot.name,
                    **_activity_names(ctx, [f["document_id"] for f in keep + new_files]),
                },
            }
        ],
        **durable_args,
    )
    if durable_confirmation:
        jobs = [replace(job, job_id=job_id) for job, job_id in zip(jobs, job_ids, strict=True)]
    _record_confirmation(ctx, slot, decided_files, actor_id)
    _release_held(ctx, slot, decided_files, actor_id)
    return jobs


def _confirm_decision(
    tenant_id: str, deal_id: str, slot_id: str, actor_id: str | None
) -> list[ExtractionJob]:
    started = time.perf_counter()
    ctx = _load(tenant_id, deal_id)
    loaded = time.perf_counter()
    slot, row = ctx.slot_by_id(slot_id)
    status, _ = display_status(ctx, slot, row)
    if status != "proposed":
        raise SlotConflict("SLOT_NOT_PROPOSED", "This slot has no proposal to confirm.")
    assigned = ctx.assigned(row["id"])
    jobs = _decide(
        ctx,
        slot,
        row,
        keep=assigned,
        new_doc=None,
        demote=[],
        action="document_slot.confirmed",
        actor_id=actor_id,
        extra_metadata={
            "proposed_confidence": row.get("confidence"),
            "match_reason": row.get("match_reason"),
        },
        match_reason=row.get("match_reason"),
        confidence=row.get("confidence"),
        durable_confirmation=True,
    )
    decided = time.perf_counter()
    logging.getLogger("uvicorn.error").info(
        "slot_confirm_decision_ms load=%.2f decision=%.2f total=%.2f",
        (loaded - started) * 1000,
        (decided - loaded) * 1000,
        (decided - started) * 1000,
    )
    return jobs


def confirm_command(tenant_id: str, deal_id: str, slot_id: str, actor_id: str | None) -> Row:
    """Acknowledge a durable decision without rebuilding every slot's checks."""
    jobs = _confirm_decision(tenant_id, deal_id, slot_id, actor_id)
    for job in jobs:
        enqueue_slot_extraction(tenant_id, deal_id, job)
    return {"kind": "slot_confirmation", "slot_id": slot_id, "status": "processing" if jobs else "confirmed"}


def confirm(
    tenant_id: str, deal_id: str, slot_id: str, actor_id: str | None
) -> tuple[Row, list[ExtractionJob]]:
    """Compatibility wrapper for clients expecting the complete board."""
    jobs = _confirm_decision(tenant_id, deal_id, slot_id, actor_id)
    board = get_board(tenant_id, deal_id)
    return board, jobs


def change(
    tenant_id: str, deal_id: str, slot_id: str, document_id: str, actor_id: str | None
) -> tuple[Row, list[ExtractionJob]]:
    """The user's choice replaces whatever the slot held; the replaced files stay offered as candidates."""
    ctx = _load(tenant_id, deal_id)
    slot, row = ctx.slot_by_id(slot_id)
    doc = _require_document(ctx, document_id)
    assigned = ctx.assigned(row["id"])
    if len(assigned) == 1 and assigned[0]["document_id"] == document_id and row["status"] in DECIDED:
        return get_board(tenant_id, deal_id), []
    removals, updates = _moves_from_other_slots(ctx, row["id"], document_id)
    demote = [f for f in assigned if f["document_id"] != document_id]
    jobs = _decide(
        ctx,
        slot,
        row,
        keep=[],
        new_doc=doc,
        demote=demote,
        action="document_slot.changed",
        actor_id=actor_id,
        extra_metadata={"previous_status": row["status"], "previous_match_reason": row.get("match_reason")},
        match_reason=CHOSEN_REASON,
        confidence=None,
        extra_removals=removals,
        extra_updates=updates,
    )
    return get_board(tenant_id, deal_id), jobs


def assign(
    tenant_id: str, deal_id: str, slot_id: str, document_id: str, actor_id: str | None
) -> tuple[Row, list[ExtractionJob]]:
    ctx = _load(tenant_id, deal_id)
    slot, row = ctx.slot_by_id(slot_id)
    doc = _require_document(ctx, document_id)
    assigned = ctx.assigned(row["id"])
    if any(f["document_id"] == document_id for f in assigned) and row["status"] in DECIDED:
        return get_board(tenant_id, deal_id), []
    removals, updates = _moves_from_other_slots(ctx, row["id"], document_id)
    status, _ = display_status(ctx, slot, row)
    others = [f for f in assigned if f["document_id"] != document_id]
    if slot.multi and status in DECIDED:
        keep, demote = others, []
    else:
        # Single slot: the new file replaces the holder. Multi slot still only proposed:
        # the other proposed files are not confirmed by this decision.
        keep, demote = [], others
    jobs = _decide(
        ctx,
        slot,
        row,
        keep=keep,
        new_doc=doc,
        demote=demote,
        action="document_slot.assigned",
        actor_id=actor_id,
        extra_metadata={"previous_status": status},
        match_reason=CHOSEN_REASON,
        confidence=None,
        extra_removals=removals,
        extra_updates=updates,
    )
    return get_board(tenant_id, deal_id), jobs


def unassign(tenant_id: str, deal_id: str, slot_id: str, document_id: str, actor_id: str | None) -> Row:
    ctx = _load(tenant_id, deal_id)
    slot, row = ctx.slot_by_id(slot_id)
    assigned = ctx.assigned(row["id"])
    target = next((f for f in assigned if f["document_id"] == document_id), None)
    if target is None:
        raise SlotConflict("DOCUMENT_NOT_IN_SLOT", "This file is not in this slot.")
    remaining = [f for f in assigned if f["document_id"] != document_id]
    updates = []
    if not remaining:
        updates.append(
            {"id": row["id"], "status": "empty", "confidence": None, "match_reason": None, "error": None}
        )
    slot_store.write_changes(
        tenant_id=tenant_id,
        deal_id=deal_id,
        actor_id=actor_id,
        slot_updates=updates,
        remove_files=[{"slot_id": row["id"], "document_id": document_id, "role": "assigned"}],
        # Kept as a candidate: the file is decided (propose will not re-place it) and still offered.
        add_files=[{**target, "role": "candidate", "match_reason": target.get("match_reason")}],
        audit=[
            {
                "action": "document_slot.unassigned",
                "entity_type": "document_slot",
                "entity_id": row["id"],
                "metadata": {
                    "slot_key": slot.key,
                    "document_id": document_id,
                    "previous_status": row["status"],
                    "slot_name": slot.name,
                    "document_name": _display_name(ctx, document_id),
                },
            }
        ],
    )
    return get_board(tenant_id, deal_id)


def retry(
    tenant_id: str, deal_id: str, slot_id: str, actor_id: str | None
) -> tuple[Row, list[ExtractionJob]]:
    ctx = _load(tenant_id, deal_id)
    slot, row = ctx.slot_by_id(slot_id)
    status, error = display_status(ctx, slot, row)
    if status != "failed":
        raise SlotConflict("SLOT_NOT_FAILED", "Only a slot whose extraction failed can be retried.")
    assigned = ctx.assigned(row["id"])
    jobs = _jobs(slot, assigned)
    slot_store.write_changes(
        tenant_id=tenant_id,
        deal_id=deal_id,
        actor_id=actor_id,
        slot_updates=[
            {
                "id": row["id"],
                "status": "processing" if jobs else "failed",
                "confidence": row.get("confidence"),
                "match_reason": row.get("match_reason"),
                "error": None if jobs else (error or "No extractor reads this slot yet."),
            }
        ],
        reset_versions=[j.version_id for j in jobs],
        audit=[
            {
                "action": "document_slot.retried",
                "entity_type": "document_slot",
                "entity_id": row["id"],
                "metadata": {"slot_key": slot.key, "error": error, "slot_name": slot.name},
            }
        ],
    )
    return get_board(tenant_id, deal_id), jobs


DOCUMENT_CITED_MESSAGE = (
    "Facts in this deal cite this document. Withdrawing it needs a review of those facts."
)


def withdraw(tenant_id: str, deal_id: str, document_id: str, actor_id: str) -> Row:
    ctx = _load(tenant_id, deal_id)
    doc = ctx.documents.get(document_id)
    if doc is None:
        raise DocumentNotFound(document_id)
    cited = citing_paths(slot_store.load_model(tenant_id, deal_id), doc)
    if cited:
        raise SlotConflict("DOCUMENT_CITED", DOCUMENT_CITED_MESSAGE)
    updates, slot_keys, slot_names = [], [], []
    for slot in ctx.slots:
        row = ctx.rows[slot.key]
        assigned = ctx.assigned(row["id"])
        if any(f["document_id"] == document_id for f in assigned):
            slot_keys.append(slot.key)
            slot_names.append(slot.name)
            if len(assigned) == 1:
                updates.append(
                    {
                        "id": row["id"],
                        "status": "withdrawn",
                        "confidence": None,
                        "match_reason": None,
                        "error": None,
                    }
                )
    # Archive through the fact ledger first: it locks the deal, archives the document and
    # supersedes any current resolution that rests on it, so withdrawn evidence stops being current.
    from creditpaper.ledger.service import archive_document

    archive_document(tenant_id, deal_id, document_id, actor_id)
    slot_store.write_changes(
        tenant_id=tenant_id,
        deal_id=deal_id,
        actor_id=actor_id,
        slot_updates=updates,
        remove_files=[{"document_id": document_id}],
        audit=[
            {
                "action": "document.withdrawn",
                "entity_type": "deal_document",
                "entity_id": document_id,
                "metadata": {
                    "document_id": document_id,
                    "slot_keys": slot_keys,
                    "filenames": _names(doc),
                    "document_name": _display_name(ctx, document_id),
                    "slot_names": slot_names,
                },
            }
        ],
    )
    return get_board(tenant_id, deal_id)


# --- extraction for a confirmed slot -----------------------------------------------


def enqueue_slot_extraction(tenant_id: str, deal_id: str, job: ExtractionJob) -> str:
    """Queue the per-document extraction with the confirmed slot's extractor (D3: the user's
    mapping, not the filename classifier, picks the extractor).

    Runs through the durable processing queue, so leases, retries and the one-running-job-per-deal
    rule apply. The resource key carries the extractor: a confirm made while the upload's own
    document job is still active queues behind it instead of being folded into it (which would
    drop the confirmed extractor).
    """
    from creditpaper.ledger import jobs

    if job.job_id is not None:
        # The job is already durable. A failed local wake-up must not report the
        # saved decision as failed; supervised workers also poll pending jobs.
        try:
            jobs.dispatch(tenant_id, job.job_id)
        except Exception:
            log.exception("Could not wake worker for durable confirmation job")
        return job.job_id

    job_id = jobs.enqueue(
        tenant_id,
        deal_id,
        "document",
        f"{job.version_id}:{job.extractor}",
        {"version_id": job.version_id, "document_id": job.document_id, "extractor": job.extractor},
    )
    jobs.dispatch(tenant_id, job_id)
    return job_id
