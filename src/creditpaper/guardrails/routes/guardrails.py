"""Guardrails API routes under /api/guardrails.

Tenant-resolved like the rest of the API. Platform safety is read-only
(code constant); only firm rules are created/edited here.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import CurrentUser, require_permission
from creditpaper.guardrails.schemas import GuardrailCreate, GuardrailUpdate
from creditpaper.guardrails.services import guardrail_service

router = APIRouter(prefix="/guardrails", tags=["guardrails"])
log = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or str(uuid.uuid4())


def _err(code: str, message: str, request_id: str, http_status: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http_status,
        detail={"error": {"code": code, "message": message, "request_id": request_id}},
    )


@router.get("", response_model=dict)
def get_guardrails(
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _perm: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    rid = _request_id(request)
    try:
        return guardrail_service.list_rules(tenant_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("list_rules failed")
        raise _err("GUARDRAILS_LIST_FAILED", "failed to list guardrails", rid, 500) from exc


@router.get("/accuracy", response_model=dict)
def get_accuracy(
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _perm: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    rid = _request_id(request)
    try:
        return guardrail_service.accuracy(tenant_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("accuracy failed")
        raise _err("ACCURACY_FAILED", "failed to compute accuracy", rid, 500) from exc


@router.post("", response_model=dict)
def post_guardrail(
    body: GuardrailCreate,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _admin: CurrentUser = Depends(require_permission("settings.update")),  # noqa: B008
):
    rid = _request_id(request)
    try:
        # Accept flat top-level fields too (direct API callers); explicit
        # `payload` keys always win.
        payload = guardrail_service.merge_flat_fields(body.payload, body.model_extra)
        return guardrail_service.create_rule(
            tenant_id,
            kind=body.kind,
            payload=payload,
            severity=body.severity,
            weight=body.weight,
            scope_type=body.scope_type,
            scope_value=body.scope_value,
        )
    except ValueError as e:
        if str(e) == "DUPLICATE_GUARDRAIL":
            raise _err(
                "DUPLICATE_GUARDRAIL",
                "Rule already exists — edit it instead",
                rid,
                409,
            ) from e
        raise _err("INVALID_GUARDRAIL", str(e), rid, 400) from e
    except Exception as exc:  # noqa: BLE001
        log.exception("create_rule failed")
        raise _err("GUARDRAIL_CREATE_FAILED", "create failed", rid, 500) from exc


@router.get("/{rule_id}", response_model=dict)
def get_guardrail(
    rule_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _perm: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    rid = _request_id(request)
    rule = guardrail_service.get_rule(tenant_id, rule_id)
    if not rule:
        raise _err("GUARDRAIL_NOT_FOUND", "rule not found", rid, 404)
    return rule


@router.patch("/{rule_id}", response_model=dict)
def patch_guardrail(
    rule_id: str,
    body: GuardrailUpdate,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _admin: CurrentUser = Depends(require_permission("settings.update")),  # noqa: B008
):
    rid = _request_id(request)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    # Same flat-field tolerance as create: merge extras into payload.
    merged = guardrail_service.merge_flat_fields(fields.pop("payload", None), body.model_extra)
    if merged:
        fields["payload"] = merged
    try:
        updated = guardrail_service.update_rule(tenant_id, rule_id, **fields)
    except ValueError as e:
        if str(e) == "DUPLICATE_GUARDRAIL":
            raise _err(
                "DUPLICATE_GUARDRAIL",
                "Rule already exists — edit it instead",
                rid,
                409,
            ) from e
        raise _err("INVALID_GUARDRAIL", str(e), rid, 400) from e
    except Exception as exc:  # noqa: BLE001
        log.exception("update_rule failed")
        raise _err("GUARDRAIL_UPDATE_FAILED", "update failed", rid, 500) from exc
    if not updated:
        raise _err("GUARDRAIL_NOT_FOUND", "rule not found", rid, 404)
    return updated


@router.delete("/{rule_id}", response_model=dict)
def delete_guardrail(
    rule_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    _admin: CurrentUser = Depends(require_permission("settings.update")),  # noqa: B008
):
    rid = _request_id(request)
    try:
        ok = guardrail_service.delete_rule(tenant_id, rule_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("delete_rule failed")
        raise _err("GUARDRAIL_DELETE_FAILED", "delete failed", rid, 500) from exc
    if not ok:
        raise _err("GUARDRAIL_NOT_FOUND", "rule not found", rid, 404)
    return {"ok": True}


@router.get("/{rule_id}/evaluations", response_model=dict)
def get_evaluations(
    rule_id: str,
    request: Request,
    tenant_id: str = Depends(resolve_tenant_id),
    limit: int = 100,
    _perm: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    rid = _request_id(request)
    try:
        return {"evaluations": guardrail_service.list_evaluations(tenant_id, rule_id, limit=min(limit, 500))}
    except Exception as exc:  # noqa: BLE001
        log.exception("list_evaluations failed")
        raise _err("EVALUATIONS_FAILED", "failed to list evaluations", rid, 500) from exc
