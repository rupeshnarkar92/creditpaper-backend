from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import CurrentUser, require_permission
from creditpaper.db import tenant_cursor
from creditpaper.ledger import contradictions, entities, retrieval, service
from creditpaper.ledger.types import FactPeriod, QueryScope, ReviewDecision

router = APIRouter(prefix="/api/ledger", tags=["ledger"])
writer = Depends(require_permission("papers.update"))
reader = Depends(require_permission("papers.read"))


class FactQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: QueryScope | None = None
    approval_id: UUID | None = None


@router.post("/deals/{deal_id}/facts/query")
def query_facts(
    deal_id: str, body: FactQuery, tenant_id: str = Depends(resolve_tenant_id), user: CurrentUser = reader
):
    if body.approval_id:
        if body.scope:
            raise HTTPException(422, "Approval and explicit time scope are mutually exclusive")
        return service.read_approval(tenant_id, deal_id, str(body.approval_id))
    scope = body.scope or QueryScope.now()
    return {"facts": service.read_facts(tenant_id, deal_id, scope), "scope": scope.model_dump(mode="json")}


@router.get("/deals/{deal_id}/assertions")
def assertions(
    deal_id: str,
    after: UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    tenant_id: str = Depends(resolve_tenant_id),
    user: CurrentUser = reader,
):
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT ledger_revision FROM deals WHERE tenant_id=%s AND id=%s", (tenant_id, deal_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Deal not found")
        revision = row[0]
        cur.execute(
            """SELECT a.id,a.field_path,a.fact,a.valid_from,a.valid_to,a.recorded_at,a.disposition
            FROM ledger_assertions a WHERE a.tenant_id=%s AND a.deal_id=%s AND (%s::uuid IS NULL OR a.id>%s)
            ORDER BY a.id LIMIT %s""",
            (tenant_id, deal_id, after, after, limit + 1),
        )
        names = [c.name for c in cur.description]
        rows = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
        return {
            "assertions": rows[:limit],
            "revision": revision,
            "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
        }


@router.post("/deals/{deal_id}/assertions/{assertion_id}/review")
def review(
    deal_id: str,
    assertion_id: UUID,
    body: ReviewDecision,
    tenant_id: str = Depends(resolve_tenant_id),
    user: CurrentUser = writer,
):
    with tenant_cursor(tenant_id) as cur:
        saved = service.decide(
            cur,
            tenant_id,
            deal_id,
            str(assertion_id),
            str(user.id),
            body.action,
            body.reason,
            body.expected_revision,
            body.period,
        )
    return {"revision": saved.model_version, "status": body.action}


class ResolveContradiction(BaseModel):
    """The analyst picks one of the sources already in the register, and says why.

    `reason` is required by the service and required again here, so a bad request
    is a 422 rather than a 500. A resolution without a stated reason is the thing
    D4 exists to prevent: the disagreement would vanish with nothing on the record
    saying who decided it or on what ground.
    """

    model_config = ConfigDict(extra="forbid")
    chosen_assertion_id: UUID
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int | None = None
    reason_code: str | None = Field(default=None, max_length=64)

    @field_validator("reason")
    @classmethod
    def _stated(cls, value: str) -> str:
        """Whitespace is not a reason.

        `min_length` alone lets "   " through to the service, which rejects it with
        a plain ValueError - not a LedgerError, so the handler does not map it and
        the caller gets a 500 for what is a bad request.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("a contradiction is resolved with a stated reason")
        return stripped


@router.get("/deals/{deal_id}/contradictions")
def list_deal_contradictions(
    deal_id: str,
    status: Literal["open", "resolved", "noted"] | None = Query(default=None),
    tenant_id: str = Depends(resolve_tenant_id),
    user: CurrentUser = reader,
):
    """The deal's register. `blocking` is the subset that holds approval.

    `contradiction_register` has been filling since 0038 and nothing could read it
    but `checks/registry.py`, so an analyst had no way to see what was holding the
    deal, let alone clear it.
    """
    rows = contradictions.list_contradictions(tenant_id, deal_id, status=status)
    return {
        "contradictions": [
            {
                "id": row.id,
                "concept_path": row.concept_path,
                "assertion_ids": list(row.assertion_ids),
                "diff_class": row.diff_class,
                "material": row.material,
                "status": row.status,
                "blocking": row.blocking,
                "resolved_value": row.resolved_value,
                "resolved_assertion_id": row.resolved_assertion_id,
                "resolved_by": row.resolved_by,
                "reason": row.reason,
                "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
                "registry_version": row.registry_version,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.post("/deals/{deal_id}/contradictions/{contradiction_id}/resolve")
def resolve_contradiction(
    deal_id: str,
    contradiction_id: UUID,
    body: ResolveContradiction,
    tenant_id: str = Depends(resolve_tenant_id),
    user: CurrentUser = writer,
):
    """Close one contradiction: the service takes the deal lock, accepts the chosen
    source, sets aside the others and writes the label. Every branch of that is the
    service's, not this route's - the route only carries who is asking."""
    saved = contradictions.resolve(
        tenant_id,
        deal_id,
        str(contradiction_id),
        chosen_assertion_id=str(body.chosen_assertion_id),
        actor_id=str(user.id),
        reason=body.reason,
        expected_revision=body.expected_revision,
        reason_code=body.reason_code,
    )
    return {"revision": saved.model_version, "status": "resolved"}


class EntityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    kind: Literal["company", "trust", "person", "partnership", "facility", "asset"]
    keys: list[tuple[str, str]] = Field(default_factory=list, max_length=8)
    deal_id: str = Field(min_length=1, max_length=100)
    assertion_id: UUID


@router.post("/entities/resolve")
def resolve(body: EntityRequest, tenant_id: str = Depends(resolve_tenant_id), user: CurrentUser = writer):
    return entities.resolve_entity(
        tenant_id,
        **body.model_dump(exclude={"assertion_id"}),
        assertion_id=str(body.assertion_id),
        actor_id=str(user.id),
    )


class MatchDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: bool
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/matches/{proposal_id}/review")
def confirm(
    proposal_id: UUID,
    body: MatchDecision,
    tenant_id: str = Depends(resolve_tenant_id),
    user: CurrentUser = writer,
):
    entities.confirm_match(tenant_id, str(proposal_id), str(user.id), body.accepted, body.reason)
    return {"status": "recorded"}


class EdgeRequest(FactPeriod):
    source_id: UUID
    target_id: UUID
    kind: Literal["controls", "borrows", "guarantees", "secured_by", "exposure"]
    deal_id: str = Field(min_length=1, max_length=100)
    assertion_id: UUID


@router.post("/relationships")
def relationship(body: EdgeRequest, tenant_id: str = Depends(resolve_tenant_id), user: CurrentUser = writer):
    if body.valid_from is None:
        raise HTTPException(422, "Relationship effective date is required")
    edge_id = entities.add_edge(
        tenant_id,
        source_id=str(body.source_id),
        target_id=str(body.target_id),
        kind=body.kind,
        deal_id=body.deal_id,
        assertion_id=str(body.assertion_id),
        actor_id=str(user.id),
        valid_from=body.valid_from,
        valid_to=body.valid_to,
    )
    return {"relationship_id": edge_id}


class Retraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/relationships/{edge_id}/retract")
def retract(
    edge_id: UUID, body: Retraction, tenant_id: str = Depends(resolve_tenant_id), user: CurrentUser = writer
):
    entities.retract_edge(tenant_id, str(edge_id), str(user.id), body.reason)
    return {"status": "retracted"}


@router.post("/matches/{proposal_id}/retract")
def retract_identity(
    proposal_id: UUID,
    body: Retraction,
    tenant_id: str = Depends(resolve_tenant_id),
    user: CurrentUser = writer,
):
    entities.retract_match(tenant_id, str(proposal_id), str(user.id), body.reason)
    return {"status": "retracted"}


class GraphQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: QueryScope | None = None
    depth: int = Field(default=2, ge=1, le=4)


@router.post("/entities/{entity_id}/exposures")
def exposures(
    entity_id: UUID, body: GraphQuery, tenant_id: str = Depends(resolve_tenant_id), user: CurrentUser = reader
):
    return entities.traverse(tenant_id, str(entity_id), body.scope or QueryScope.now(), body.depth)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    scope: QueryScope | None = None
    vector: list[float] | None = Field(default=None, min_length=1, max_length=4096)
    model: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)


@router.post("/deals/{deal_id}/search")
def search(
    deal_id: str, body: SearchRequest, tenant_id: str = Depends(resolve_tenant_id), user: CurrentUser = reader
):
    return retrieval.search_documents(
        tenant_id,
        deal_id,
        body.query,
        body.scope or QueryScope.now(),
        vector=body.vector,
        model=body.model,
        limit=body.limit,
    )


@router.get("/deals/{deal_id}/jobs/{job_id}")
def processing_status(
    deal_id: str, job_id: UUID, tenant_id: str = Depends(resolve_tenant_id), user: CurrentUser = reader
):
    from creditpaper.ledger.jobs import status

    return status(tenant_id, deal_id, str(job_id))


class RetrievalQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["relationships", "facts", "narrative"]
    deal_id: str | None = Field(default=None, min_length=1, max_length=100)
    entity_id: UUID | None = None
    scope: QueryScope | None = None
    approval_id: UUID | None = None
    query: str = Field(default="", max_length=2000)
    depth: int = Field(default=2, ge=1, le=4)
    vector: list[float] | None = Field(default=None, min_length=1, max_length=4096)
    model: str | None = Field(default=None, max_length=200)


@router.post("/query")
def retrieve(body: RetrievalQuery, tenant_id: str = Depends(resolve_tenant_id), user: CurrentUser = reader):
    """Explicit query intent keeps document similarity from answering factual questions."""
    if body.approval_id:
        if body.scope or not body.deal_id:
            raise HTTPException(422, "Approval scope requires a deal and no explicit time scope")
        approval = service.read_approval(tenant_id, body.deal_id, str(body.approval_id))
        scope = QueryScope(valid_at=approval["valid_at"], known_at=approval["known_at"], include_undated=True)
    else:
        scope = body.scope or QueryScope.now()
    if body.intent == "relationships":
        if not body.entity_id:
            raise HTTPException(422, "Entity is required for relationship retrieval")
        return entities.traverse(tenant_id, str(body.entity_id), scope, body.depth)
    if not body.deal_id:
        raise HTTPException(422, "Deal is required")
    if body.intent == "facts":
        return {
            "facts": service.read_facts(tenant_id, body.deal_id, scope),
            "scope": scope.model_dump(mode="json"),
        }
    if not body.query.strip():
        raise HTTPException(422, "Narrative query is required")
    return retrieval.search_documents(
        tenant_id, body.deal_id, body.query, scope, vector=body.vector, model=body.model
    )
