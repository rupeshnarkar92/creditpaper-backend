"""HTTP routes for the deals domain.

CRUD endpoints (deals list/management):
- GET    /deals
- GET    /deals/{id}
- POST   /deals
- PUT    /deals/{id}
- DELETE /deals/{id}

Section/editor endpoints (review surface):
- GET    /deals/{id}/sections
- GET    /deals/{id}/sections/{n}
- POST   /deals/{id}/sections/{n}/signals
- GET    /deals/{id}/sections/{n}/signals
- POST   /deals/{id}/render
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import get_current_user, require_any_permission, require_permission
from creditpaper.auth.models.user import User
from creditpaper.contracts import Authorship, Signal, SignalKind
from creditpaper.db import tenant_cursor, tenant_uuid
from creditpaper.deals.routes.deals import list_deal_ids, load_prose
from creditpaper.deals.routes.paper import deal_logo_path, template_unavailable
from creditpaper.deals.services.activity import record_deal_event
from creditpaper.deals.services.deal_templates import (
    STANDARD_ID,
    TemplateUnavailable,
    effective_template,
    resolve_for_new_deal,
    template_ref,
)
from creditpaper.deals.store import deal_template_store, deals_list_store
from creditpaper.deals.utils.prose_blocks import ProseBlockOut, parse_prose_blocks
from creditpaper.deals.utils.render_reviewed import render_reviewed_paper
from creditpaper.paper.logo_fetch import fetch_logo
from creditpaper.paper.theme import brand_logo_url, get_theme, load_brand
from creditpaper.renderer.paper_spec import DOCX_IMAGE_TYPES, PaperSpec
from creditpaper.signals.utils.store import append_signal, latest_edit_text, read_signals

logger = logging.getLogger(__name__)

# Permission gates, not role gates: the API decides on grants, so widening or
# narrowing a role is a seed change and needs no route edit (2026-09-25 ruling).
_can_create_deal = Depends(require_permission("papers.create"))
_can_update_deal = Depends(require_permission("papers.update"))
_can_delete_deal = Depends(require_permission("papers.delete"))
# A review comment is the checker's (`papers.comment`) and the drafter's — makers
# could always leave one, so the gate is additive rather than a swap.
_can_comment = Depends(require_any_permission("papers.comment", "papers.update"))
_signed_in = Depends(get_current_user)

# Decision states are terminal for deletion: mirrors workspace._DECISION_STATES,
# which is the state machine that owns reaching them (approving needs
# papers.approve, so this is the same two values by construction).
_FINAL_STATES = {"APPROVED", "ISSUED"}

_DEAL_COLUMNS = """
    id, tenant_id, name, borrower, deal_type, sector, address,
    thesis_content, thesis_status, owner_id,
    facility_amount, document_count, document_required,
    created_at, updated_at, template_id, template_source, template_snapshot,
    current_state
"""

router = APIRouter(prefix="/api/deals", tags=["deals"])


# === CRUD Schemas ===


class DealResponse(BaseModel):
    id: str
    tenant_id: str
    name: str | None = None
    borrower: str | None = None
    deal_type: str | None = None
    sector: str | None = None
    address: str | None = None
    thesis_content: str | None = None
    thesis_status: str | None = None
    owner_id: str | None = None
    facility_amount: float | None = None
    document_count: int = 0
    document_required: int = 25
    created_at: str | None = None
    updated_at: str | None = None
    # The lifecycle state machine's value (state_machine.LEGAL_TRANSITIONS) — the single
    # source of truth for where a deal is. The `status` and `stage` columns that used to
    # shadow it were INSERT-time copies, never updated, and were dropped (they showed
    # Draft forever). Anything that shows a deal's position must read this. There are 12
    # states; the UI's badge collapses them (see stateToStatus).
    current_state: str | None = None
    # The deal's template (spec 2026-09-15 §2). Null on deals created before templates
    # were stored on a deal; those resolve at read time via GET /paper/sections.
    template_id: str | None = None
    template_source: str | None = None
    template_name: str | None = None


class DealListResponse(BaseModel):
    rows: list[DealResponse]
    total: int
    page: int
    limit: int
    pages: int


class CreateDealRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    borrower: str = Field(..., min_length=1, max_length=200)
    # Facility type, as model/deal.py DealType: it decides the deal's document slots
    # (docs/specs/slot-taxonomy) and which checks and missing-document rules apply.
    deal_type: str = Field(..., pattern=r"^(construction|bridge|residual_stock)$")
    facility_amount: float = Field(..., ge=0)
    # A firm template UUID or "platform:standard". Absent: the server applies the
    # resolution rule (type match, firm default, Standard).
    template_id: str | None = Field(None, min_length=1, max_length=64)


class UpdateDealRequest(BaseModel):
    """Writable deal fields.

    Deliberately no lifecycle state: it is the state machine's, reached through the
    transitions route, which needs `papers.approve`. Letting a client write it would
    let any `papers.update` holder mark a deal approved while the real state never
    moved.
    """

    name: str | None = Field(None, min_length=1, max_length=200)
    borrower: str | None = Field(None, min_length=1, max_length=200)
    deal_type: str | None = Field(None, pattern=r"^(construction|bridge|residual_stock)$")
    facility_amount: float | None = Field(None, ge=0)


def _deal_row_to_response(row) -> DealResponse:
    return DealResponse(
        id=row[0],
        tenant_id=str(row[1]),
        name=row[2],
        borrower=row[3],
        deal_type=row[4],
        sector=row[5],
        address=row[6],
        thesis_content=row[7],
        thesis_status=row[8],
        owner_id=str(row[9]) if row[9] else None,
        facility_amount=float(row[10]) if row[10] is not None else None,
        document_count=row[11] or 0,
        document_required=row[12] or 25,
        created_at=row[13].isoformat() if row[13] else None,
        updated_at=row[14].isoformat() if row[14] else None,
        # Appended after the template columns so the positional indices above are
        # unchanged; absent only if a caller selects a narrower column list.
        current_state=row[18] if len(row) > 18 else None,
        **_template_fields(row),
    )


def _template_fields(row) -> dict:
    if len(row) < 18:
        return {}
    snapshot = row[17] if isinstance(row[17], dict) else None
    source = row[16]
    template_id = str(row[15]) if row[15] else None
    if template_id is None and snapshot is not None:
        template_id = STANDARD_ID
    return {
        "template_id": template_id,
        "template_source": source,
        "template_name": snapshot.get("name") if snapshot else None,
    }


def _with_document_counts(tenant_id: str, deals: list[DealResponse]) -> list[DealResponse]:
    """`document_required` / `document_count` from the slot board summary (required,
    confirmed_required) for deals with a facility type. The stored columns stay as they
    are and are only the fallback: a deal without a facility type, or a database without
    the slot tables yet."""
    from creditpaper.deals.services.slot_board import document_counts

    try:
        counts = document_counts(tenant_id, [(d.id, d.deal_type) for d in deals])
    except Exception:  # noqa: BLE001 — the deal list must not fail over a count
        logger.warning("document counts unavailable; using stored columns")
        return deals
    for deal in deals:
        if deal.id in counts:
            deal.document_required, deal.document_count = counts[deal.id]
    return deals


def _generate_deal_id() -> str:
    return f"deal_{uuid.uuid4().hex[:12]}"


# === CRUD Endpoints ===


@router.get("", response_model=list[DealResponse])
def list_deals(tenant_id: str = Depends(resolve_tenant_id)):
    """List all deals for the current tenant, newest first."""
    return [DealResponse.model_validate(row) for row in deals_list_store.list_deals(tenant_id)]


@router.get("/list", response_model=DealListResponse)
def list_deals_page(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: str = Query("", max_length=200),
    stage: str = Query("", pattern=r"^(|draft|in_review|needs_attention|approved)$"),
    sort_by: str = Query("updated_at", max_length=40),
    sort_dir: str = Query("desc", pattern=r"^(asc|desc)$"),
    tenant_id: str = Depends(resolve_tenant_id),
):
    """Server-side Deals list used by the standardized list UI."""
    result = deals_list_store.list_deals_page(
        tenant_id,
        page=page,
        limit=limit,
        search=search,
        stage=stage,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    return {
        **result,
        "rows": [DealResponse.model_validate(row) for row in result["rows"]],
    }


@router.get("/{deal_id}", response_model=DealResponse)
def get_deal(deal_id: str, tenant_id: str = Depends(resolve_tenant_id)):
    """Get a single deal by id."""
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """
            SELECT id, tenant_id, name, borrower, deal_type, sector, address,
                   thesis_content, thesis_status, owner_id,
                   facility_amount, document_count, document_required,
                   created_at, updated_at, template_id, template_source, template_snapshot,
                   current_state
            FROM deals
            WHERE id = %s AND tenant_id = %s AND deleted_at IS NULL
            """,
            (deal_id, tenant_uuid(tenant_id)),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Deal not found")
    return _with_document_counts(tenant_id, [_deal_row_to_response(row)])[0]


@router.post("", response_model=DealResponse, status_code=201)
def create_deal(
    body: CreateDealRequest,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _can_create_deal,
):
    """Create a new deal with its template snapshot."""
    try:
        template_id, template_source, snapshot = resolve_for_new_deal(
            tenant_id, body.deal_type, body.template_id
        )
    except TemplateUnavailable:
        raise template_unavailable(request) from None
    deal_id = _generate_deal_id()
    now = datetime.now(UTC)
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """
            INSERT INTO deals (
                id, tenant_id, name, borrower, deal_type,
                facility_amount, document_count, document_required,
                created_at, updated_at,
                template_id, template_source, template_snapshot
            )
            VALUES (%s, %s, %s, %s, %s, %s, 0, 25, %s, %s, %s, %s, %s::jsonb)
            RETURNING id, tenant_id, name, borrower, deal_type, sector, address,
                      thesis_content, thesis_status, owner_id,
                      facility_amount, document_count, document_required,
                      created_at, updated_at, template_id, template_source, template_snapshot,
                      current_state
            """,
            (
                deal_id,
                tenant_uuid(tenant_id),
                body.name,
                body.borrower,
                body.deal_type,
                Decimal(str(body.facility_amount)),
                now,
                now,
                template_id,
                template_source,
                json.dumps(snapshot),
            ),
        )
        row = cur.fetchone()
        # Same transaction as the insert: a deal never exists without its created event.
        record_deal_event(
            cur,
            tenant_id=tenant_uuid(tenant_id),
            deal_id=deal_id,
            actor_id=user.id,
            action="deal.created",
            entity_type="deal",
            entity_id=deal_id,
            metadata={
                "name": body.name,
                "deal_type": body.deal_type,
                "template_id": template_id,
                "template_source": template_source,
            },
            created_at=now,
        )
    return _with_document_counts(tenant_id, [_deal_row_to_response(row)])[0]


@router.put("/{deal_id}", response_model=DealResponse)
def update_deal(
    deal_id: str,
    body: UpdateDealRequest,
    tenant_id: str = Depends(resolve_tenant_id),
    _writer: User = _can_update_deal,
):
    """Update an existing deal."""
    updates = []
    params = []
    if body.name is not None:
        updates.append("name = %s")
        params.append(body.name)
    if body.borrower is not None:
        updates.append("borrower = %s")
        params.append(body.borrower)
    if body.deal_type is not None:
        updates.append("deal_type = %s")
        params.append(body.deal_type)
    if body.facility_amount is not None:
        updates.append("facility_amount = %s")
        params.append(Decimal(str(body.facility_amount)))

    if not updates:
        return get_deal(deal_id, tenant_id)

    updates.append("updated_at = %s")
    params.append(datetime.now(UTC))
    params.append(deal_id)
    params.append(tenant_uuid(tenant_id))

    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            f"""
            UPDATE deals
            SET {", ".join(updates)}
            WHERE id = %s AND tenant_id = %s AND deleted_at IS NULL
            RETURNING id, tenant_id, name, borrower, deal_type, sector, address,
                      thesis_content, thesis_status, owner_id,
                      facility_amount, document_count, document_required,
                      created_at, updated_at, template_id, template_source, template_snapshot,
                      current_state
            """,
            params,
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Deal not found")
    return _with_document_counts(tenant_id, [_deal_row_to_response(row)])[0]


@router.delete("/{deal_id}")
def delete_deal(
    deal_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _can_delete_deal,
):
    """Archive a deal (soft delete, rn_008).

    The row, its documents and its audit trail survive; only the reads stop
    returning it. A deal that reached a decision state is never archived — the
    record of an approved paper does not disappear. The event is written in the
    same transaction as the archive, so an archived deal always carries who did
    it. Hard delete was removed because it cascaded the audit trail away and
    raised a foreign key violation on any deal holding sections or ledger rows.
    """
    now = datetime.now(UTC)
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """
            UPDATE deals
               SET deleted_at = %s, updated_at = %s
             WHERE id = %s AND tenant_id = %s AND deleted_at IS NULL
            RETURNING name, current_state
            """,
            (now, now, deal_id, tid),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Deal not found")
        name, state = row
        # `current_state` is the state machine's value (stateToStatus collapses it for
        # display); it is the only field that has ever held a decision state.
        if state in _FINAL_STATES:
            # Raising inside the block rolls the UPDATE back, so the deal is untouched.
            raise HTTPException(status_code=409, detail="An approved deal cannot be deleted")
        record_deal_event(
            cur,
            tenant_id=tid,
            deal_id=deal_id,
            actor_id=user.id,
            action="deal.deleted",
            entity_type="deal",
            entity_id=deal_id,
            metadata={"name": name, "current_state": state},
            created_at=now,
        )
    return {"ok": True, "deleted": deal_id}


# === Section/Editor Schemas (existing) ===


class SectionSummary(BaseModel):
    section: int
    status: str
    authorship: str
    char_count: int
    citation_count: int
    signal_count: int
    is_edited: bool


class SectionDetail(BaseModel):
    section: int
    status: str
    authorship: str
    is_edited: bool
    blocks: list[ProseBlockOut]
    original_blocks: list[ProseBlockOut]
    cited_field_paths: list[str]
    signals: list[Signal]


class SignalIn(BaseModel):
    kind: SignalKind
    author: str
    comment_text: str | None = None
    anchored_text: str | None = None
    before_text: str | None = None
    after_text: str | None = None


class RenderResult(BaseModel):
    path: str


def _authorship_of(block: dict, edited: str | None) -> str:
    if edited is not None:
        return Authorship.HUMAN_EDITED.value
    return str(block.get("authorship", Authorship.AI_GENERATED.value))


def _require_prose(deal_id: str, tenant_id: str) -> dict[str, dict]:
    prose = load_prose(deal_id, tenant_id=tenant_id)
    if prose is None:
        raise HTTPException(status_code=404, detail=f"no cached prose for deal '{deal_id}'")
    return prose


def _may_change_prose(user: User) -> bool:
    """Whether this person may change section text, as opposed to remark on it.

    A review comment and an edit are different acts with different grants: the
    reviewer comments (`papers.comment`), the drafter edits (`papers.update`).
    """
    return bool(user.is_super_admin) or "papers.update" in user.permissions


# Legacy editor endpoint (returns list of deal IDs for the review surface)
# This avoids conflicting with the CRUD list_deals above.
# Mounted at /editor/deals in main.py
editor_router = APIRouter(prefix="/api/editor/deals", tags=["deals-editor"])


@editor_router.get("", response_model=list[str])
def get_editor_deals(tenant_id: str = Depends(resolve_tenant_id)) -> list[str]:
    return list_deal_ids(tenant_id)


@editor_router.get("/{deal_id}/sections", response_model=list[SectionSummary])
def get_sections(deal_id: str, tenant_id: str = Depends(resolve_tenant_id)) -> list[SectionSummary]:
    prose = _require_prose(deal_id, tenant_id)
    out = []
    for key in sorted(prose, key=int):
        block = prose[key]
        section = block["section"]
        edited = latest_edit_text(deal_id, section, tenant_id=tenant_id)
        out.append(
            SectionSummary(
                section=section,
                status="human-edited" if edited is not None else block["status"],
                authorship=_authorship_of(block, edited),
                char_count=len(edited if edited is not None else block["text"]),
                citation_count=len(block.get("cited_field_paths", [])),
                signal_count=len(read_signals(deal_id, section=section, tenant_id=tenant_id)),
                is_edited=edited is not None,
            )
        )
    return out


@editor_router.get("/{deal_id}/sections/{section}", response_model=SectionDetail)
def get_section(deal_id: str, section: int, tenant_id: str = Depends(resolve_tenant_id)) -> SectionDetail:
    prose = _require_prose(deal_id, tenant_id)
    block = prose.get(str(section))
    if not block:
        raise HTTPException(status_code=404, detail=f"no section {section} for deal '{deal_id}'")
    edited = latest_edit_text(deal_id, section, tenant_id=tenant_id)
    return SectionDetail(
        section=block["section"],
        status="human-edited" if edited is not None else block["status"],
        authorship=_authorship_of(block, edited),
        is_edited=edited is not None,
        blocks=parse_prose_blocks(edited if edited is not None else block["text"]),
        original_blocks=parse_prose_blocks(block["text"]),
        cited_field_paths=block.get("cited_field_paths", []),
        signals=read_signals(deal_id, section=section, tenant_id=tenant_id),
    )


@editor_router.post("/{deal_id}/sections/{section}/signals", response_model=Signal)
def post_signal(
    deal_id: str,
    section: int,
    body: SignalIn,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _can_comment,
) -> Signal:
    if body.kind == SignalKind.EDIT and not _may_change_prose(user):
        raise HTTPException(status_code=403, detail="Editing section text needs papers.update")
    _require_prose(deal_id, tenant_id)
    signal = Signal(
        deal_id=deal_id,
        section=section,
        tenant_id=tenant_id,
        author=str(user.id),
        **body.model_dump(exclude={"author"}),
    )
    return append_signal(signal)


@editor_router.get("/{deal_id}/sections/{section}/signals", response_model=list[Signal])
def get_signals(deal_id: str, section: int, tenant_id: str = Depends(resolve_tenant_id)) -> list[Signal]:
    return read_signals(deal_id, section=section, tenant_id=tenant_id)


@editor_router.post("/{deal_id}/render", response_model=RenderResult)
def post_render(
    deal_id: str,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _signed_in,
) -> RenderResult:
    try:
        theme_payload = get_theme(tenant_id, logo_endpoint=deal_logo_path(deal_id))
    except Exception:  # noqa: BLE001 — an export never fails over styling
        theme_payload = None
    logo = None
    if theme_payload and theme_payload.get("logo"):
        url = brand_logo_url(load_brand(tenant_id))
        logo = fetch_logo(url) if url else None
    spec = (
        PaperSpec.from_theme(
            theme_payload,
            logo_bytes=logo.content if logo and logo.content_type in DOCX_IMAGE_TYPES else None,
        )
        if theme_payload
        else None
    )
    try:
        out_path = render_reviewed_paper(deal_id, spec=spec, tenant_id=tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    _audit_export(tenant_id, deal_id, theme_payload, actor_id=str(user.id))
    return RenderResult(path=str(out_path))


def _audit_export(tenant_id: str, deal_id: str, theme_payload: dict | None, *, actor_id: str) -> None:
    """Record the theme version and template reference on the deal's audit trail,
    when the deal is a stored deal of this tenant."""
    try:
        deal = deal_template_store.get_deal(tenant_id, deal_id)
        if deal is None:
            return
        template_id, source, snapshot = effective_template(tenant_id, deal)
        deal_template_store.insert_audit_event(
            tenant_id=tenant_id,
            deal_id=deal_id,
            actor_id=actor_id,
            action="paper.exported",
            metadata={
                "theme_version": (theme_payload or {}).get("version"),
                "template": {**template_ref(template_id, snapshot), "source": source},
            },
        )
    except Exception:  # noqa: BLE001 — the export has already been written
        logger.warning("export audit entry could not be written for deal %s", deal_id)
