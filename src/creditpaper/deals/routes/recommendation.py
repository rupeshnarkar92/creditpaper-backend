"""Section 9 recommendation routes (D8, ruled 2026-09-15). Mounted under `/api/deals` through
the workspace router, so `api/main.py` gains no new import.

  GET  /api/deals/{deal_id}/recommendation   any member of the tenant
  POST /api/deals/{deal_id}/recommendation   who can edit the paper (maker, tenant_admin)

Written and signed by a person; never drafted by AI. Each POST is a new signed version.
Another tenant's deal answers 404 DEAL_NOT_FOUND.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import require_permission
from creditpaper.auth.models.user import User
from creditpaper.deals.services import recommendation as service
from creditpaper.ledger.types import Conflict, NotFound

router = APIRouter(tags=["deal-recommendation"])
# Ruled 2026-10-02: the signer is the drafter, never the approver. Approval is
# `papers.approve` (checker) and signing is `papers.update` (maker, tenant_admin),
# so the two are necessarily different people and the AFSL segregation-of-duties
# control holds by construction rather than by review. A checker reads the signed
# recommendation and approves the paper; they do not author it.
_writer = Depends(require_permission("papers.update"))


class RecommendationAuthor(BaseModel):
    id: str
    name: str


class Recommendation(BaseModel):
    id: str
    body: str
    author: RecommendationAuthor
    signed_at: str | None
    ledger_revision: int | None


class RecommendationState(BaseModel):
    current: Recommendation | None
    history_count: int


class SignRecommendationRequest(BaseModel):
    body: str = ""


def _err(request: Request, code: str, message: str, status: int, field: str | None = None) -> HTTPException:
    body: dict[str, object] = {
        "code": code,
        "message": message,
        "request_id": request.headers.get("x-request-id", ""),
    }
    if field:
        body["field"] = field
    return HTTPException(status_code=status, detail={"error": body})


@router.get("/{deal_id}/recommendation", response_model=RecommendationState)
def get_recommendation(
    deal_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
) -> dict[str, Any]:
    try:
        return service.get_recommendation(tenant_id, deal_id)
    except (NotFound, ValueError):
        raise _err(request, "DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None


@router.post("/{deal_id}/recommendation", response_model=Recommendation)
def sign_recommendation(
    deal_id: str,
    body: SignRecommendationRequest,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    user: User = _writer,
) -> dict[str, Any]:
    try:
        return service.sign_recommendation(tenant_id, deal_id, str(user.id), body.body)
    except service.RecommendationEmpty as exc:
        raise _err(request, "RECOMMENDATION_EMPTY", str(exc), 422, "body") from None
    except service.RecommendationTooLong as exc:
        raise _err(request, "RECOMMENDATION_TOO_LONG", str(exc), 422, "body") from None
    except NotFound:
        raise _err(request, "DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None
    except Conflict as exc:
        raise _err(request, "RECOMMENDATION_LOCKED", str(exc), 409) from None
    except ValueError:  # a malformed tenant or user id
        raise _err(request, "DEAL_NOT_FOUND", f"deal {deal_id} not found", 404) from None
