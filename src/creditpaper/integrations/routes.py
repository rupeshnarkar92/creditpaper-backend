"""Admin control plane for external integrations, MCP access and AI gateway policy."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import unquote, urljoin, urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from creditpaper.auth.dependencies import ADMIN_ONLY, CurrentUser
from creditpaper.db import tenant_cursor
from creditpaper.integrations import store
from creditpaper.integrations.security import (
    safe_details,
    secret_fields,
    validate_integration_config,
    validate_public_https_url,
    verify_webhook_signature,
)
from creditpaper.integrations.service_auth import verify_service_request

router = APIRouter(prefix="/integrations", tags=["integrations"])


class IntegrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    provider: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=500)
    auth_type: Literal["api_key", "bearer", "oauth_client_credentials", "webhook"]
    credentials: dict[str, str] | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=6000)

    @field_validator("name", "provider")
    @classmethod
    def clean_labels(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Integration name and provider are required")
        return value

    @staticmethod
    def validate_auth_credentials(auth_type: str, credentials: dict[str, str] | None) -> None:
        if not credentials:
            raise ValueError("Integration credentials are required")
        allowed = {
            "api_key": {"api_key", "token", "access_token"},
            "bearer": {"token", "access_token"},
            "oauth_client_credentials": {"client_id", "client_secret"},
            "webhook": {"webhook_secret"},
        }[auth_type]
        if set(credentials) - allowed:
            raise ValueError("Integration credentials do not match the selected authentication type")
        if auth_type == "api_key" and not any(
            credentials.get(field) for field in ("api_key", "token", "access_token")
        ):
            raise ValueError("API key credentials must include api_key or token")
        if auth_type == "bearer" and not any(credentials.get(field) for field in ("token", "access_token")):
            raise ValueError("Bearer credentials must include token or access_token")
        if auth_type == "oauth_client_credentials" and not all(
            credentials.get(field) for field in ("client_id", "client_secret")
        ):
            raise ValueError("OAuth credentials must include client_id and client_secret")
        if auth_type == "webhook" and not credentials.get("webhook_secret"):
            raise ValueError("Webhook credentials must include webhook_secret")


class McpServerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    endpoint: str = Field(min_length=1, max_length=500)
    auth_type: Literal["none", "bearer", "api_key"] = "none"
    credentials: dict[str, str] | None = None
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    allowed_resources: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("MCP server name is required")
        return value

    @field_validator("allowed_tools", "allowed_resources")
    @classmethod
    def clean_allowlist(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("MCP allowlist entries must be non-empty and unique")
        return cleaned


class ModelPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=160)
    fallback_model: str | None = Field(default=None, max_length=160)
    allowed_models: list[str] = Field(default_factory=list, max_length=50)
    daily_token_limit: int | None = Field(default=None, gt=0, le=10_000_000_000)
    monthly_cost_limit_usd: float | None = Field(default=None, gt=0, le=10_000_000)
    max_output_tokens: int = Field(default=8192, ge=1, le=100_000)
    api_key: str | None = Field(default=None, max_length=4096)
    provider: Literal["openai", "anthropic", "google", "compatible"] | None = None
    base_url: str | None = Field(default=None, max_length=500)
    input_price_per_million_usd: float | None = Field(default=None, ge=0, le=1_000_000)
    output_price_per_million_usd: float | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("api_key")
    @classmethod
    def strip_api_key(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None

    @field_validator("model", "fallback_model")
    @classmethod
    def strip_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Model identifiers must not be empty")
        return value

    @field_validator("allowed_models")
    @classmethod
    def clean_allowed_models(cls, value: list[str]) -> list[str]:
        cleaned = [model.strip() for model in value]
        if any(not model for model in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("Allowed models must be non-empty and unique")
        return cleaned

    @model_validator(mode="after")
    def validate_model_policy(self):
        if self.model in self.allowed_models:
            raise ValueError("The primary model must not be repeated in allowed_models")
        if self.fallback_model and self.fallback_model not in self.allowed_models:
            raise ValueError("The fallback model must be included in allowed_models")
        return self


async def _secret_input(request: Request, schema: type[BaseModel]) -> BaseModel:
    """Parse secret-bearing request models without returning submitted values in 422s."""
    try:
        incoming = await request.json()
        if not isinstance(incoming, dict):
            raise ValueError("Expected a JSON object")
        return schema.model_validate(incoming)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid integration settings") from None


def _tenant(user: CurrentUser) -> str:
    if not user.tenant_id:
        raise HTTPException(status_code=403, detail="Organisation membership required")
    return str(user.tenant_id)


def _admin_identity(user: CurrentUser) -> tuple[str, str]:
    return _tenant(user), user.id


def _not_found_or_bad_input(exc: ValueError) -> HTTPException:
    if str(exc).endswith("not found"):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def _public_model_policy(tenant_id: str) -> dict[str, Any]:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """SELECT provider, model, base_url, fallback_model, allowed_models,
                      daily_token_limit, monthly_cost_limit_usd, max_output_tokens,
                      input_price_per_million_usd, output_price_per_million_usd, updated_at
                 FROM tenant_llm_providers WHERE tenant_id=%s AND enabled=true""",
            (tenant_id,),
        )
        row = cur.fetchone()
        daily_tokens, monthly_cost = 0, 0.0
        if row:
            cur.execute(
                """SELECT coalesce(sum(input_tokens + output_tokens), 0)
                     FROM tenant_llm_usage_events WHERE tenant_id=%s
                       AND created_at >= date_trunc('day', now())""",
                (tenant_id,),
            )
            daily_tokens = int(cur.fetchone()[0])
            cur.execute(
                """SELECT coalesce(sum(cost_usd), 0) FROM tenant_llm_usage_events
                     WHERE tenant_id=%s AND created_at >= date_trunc('month', now())""",
                (tenant_id,),
            )
            monthly_cost = float(cur.fetchone()[0])
    if not row:
        return {
            "configured": False,
            "provider": None,
            "model": None,
            "fallback_model": None,
            "allowed_models": [],
            "daily_token_limit": None,
            "monthly_cost_limit_usd": None,
            "max_output_tokens": 8192,
            "daily_tokens_used": 0,
            "monthly_cost_usd": 0.0,
        }
    return {
        "configured": True,
        "provider": row[0],
        "model": row[1],
        "base_url": row[2],
        "fallback_model": row[3],
        "allowed_models": list(row[4] or []),
        "daily_token_limit": row[5],
        "monthly_cost_limit_usd": float(row[6]) if row[6] is not None else None,
        "max_output_tokens": int(row[7]),
        "daily_tokens_used": daily_tokens,
        "monthly_cost_usd": monthly_cost,
        "input_price_per_million_usd": float(row[8]) if row[8] is not None else None,
        "output_price_per_million_usd": float(row[9]) if row[9] is not None else None,
        "updated_at": row[10].isoformat() if row[10] else None,
    }


@router.get("")
async def get_control_plane(user: CurrentUser = Depends(ADMIN_ONLY)):  # noqa: B008
    tenant_id = _tenant(user)
    try:
        integrations, mcp, ai = await run_in_threadpool(
            lambda: (
                store.list_integrations(tenant_id),
                store.list_mcp_servers(tenant_id),
                _public_model_policy(tenant_id),
            )
        )
        with tenant_cursor(tenant_id) as cur:
            cur.execute(
                """SELECT id, actor_id, actor_service, resource_type, resource_id, action, details, created_at
                     FROM tenant_integration_audit_events WHERE tenant_id=%s
                     ORDER BY created_at DESC LIMIT 100""",
                (tenant_id,),
            )
            events = cur.fetchall()
        return {
            "integrations": integrations,
            "mcp_servers": mcp,
            "ai": ai,
            "audit_events": [
                {
                    "id": r[0],
                    "actor_id": str(r[1]) if r[1] else None,
                    "actor_service": r[2],
                    "resource_type": r[3],
                    "resource_id": str(r[4]) if r[4] else None,
                    "action": r[5],
                    "details": safe_details(r[6]),
                    "created_at": r[7].isoformat() if r[7] else None,
                }
                for r in events
            ],
        }
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Integration settings are temporarily unavailable"
        ) from exc


@router.post("")
async def create_integration(request: Request, user: CurrentUser = Depends(ADMIN_ONLY)):  # noqa: B008
    body = await _secret_input(request, IntegrationInput)
    tenant_id, actor = _admin_identity(user)
    try:
        credentials = secret_fields(body.credentials, label="Integration credentials")
        body.validate_auth_credentials(body.auth_type, credentials)
        config = validate_integration_config(body.config, auth_type=body.auth_type)
        if body.auth_type == "oauth_client_credentials" and "token_url" not in config:
            raise ValueError("OAuth integrations require config.token_url")
        if body.auth_type == "webhook" and body.provider.lower() not in {"webhook", "custom_webhook"}:
            raise ValueError("Webhook authentication is only valid for webhook integrations")
        return await run_in_threadpool(
            store.upsert_integration,
            tenant_id,
            actor,
            resource_id=None,
            name=body.name.strip(),
            provider=body.provider.strip(),
            base_url=body.base_url,
            auth_type=body.auth_type,
            credentials=credentials,
            config=config,
            rate_limit_per_minute=body.rate_limit_per_minute,
        )
    except ValueError as exc:
        raise _not_found_or_bad_input(exc) from None
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Integration could not be saved") from exc


@router.put("/{integration_id}")
async def update_integration(
    integration_id: str,
    request: Request,
    user: CurrentUser = Depends(ADMIN_ONLY),  # noqa: B008
):
    body = await _secret_input(request, IntegrationInput)
    tenant_id, actor = _admin_identity(user)
    try:
        credentials = (
            secret_fields(body.credentials, label="Integration credentials")
            if body.credentials is not None
            else None
        )
        current = await run_in_threadpool(store.integration_runtime_config, tenant_id, integration_id)
        if not current:
            raise ValueError("Integration not found")
        if credentials is not None:
            body.validate_auth_credentials(body.auth_type, credentials)
        elif current["auth_type"] != body.auth_type or current["provider"] != body.provider.strip():
            raise ValueError("Credentials are required when changing integration provider or authentication")
        config = validate_integration_config(body.config, auth_type=body.auth_type)
        if body.auth_type == "oauth_client_credentials" and "token_url" not in config:
            raise ValueError("OAuth integrations require config.token_url")
        if body.auth_type == "webhook" and body.provider.lower() not in {"webhook", "custom_webhook"}:
            raise ValueError("Webhook authentication is only valid for webhook integrations")
        return await run_in_threadpool(
            store.upsert_integration,
            tenant_id,
            actor,
            resource_id=integration_id,
            name=body.name.strip(),
            provider=body.provider.strip(),
            base_url=body.base_url,
            auth_type=body.auth_type,
            credentials=credentials,
            config=config,
            rate_limit_per_minute=body.rate_limit_per_minute,
        )
    except ValueError as exc:
        raise _not_found_or_bad_input(exc) from None
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Integration could not be updated") from exc


@router.delete("/{integration_id}")
async def remove_integration(integration_id: str, user: CurrentUser = Depends(ADMIN_ONLY)):  # noqa: B008
    tenant_id, actor = _admin_identity(user)
    try:
        await run_in_threadpool(store.delete_integration, tenant_id, actor, integration_id)
        return {"removed": True}
    except ValueError as exc:
        raise _not_found_or_bad_input(exc) from None


@router.post("/mcp")
async def create_mcp_server(request: Request, user: CurrentUser = Depends(ADMIN_ONLY)):  # noqa: B008
    body = await _secret_input(request, McpServerInput)
    tenant_id, actor = _admin_identity(user)
    try:
        if body.auth_type == "none" and body.credentials is not None:
            raise ValueError("Credentials are not allowed with auth_type=none")
        credentials = (
            secret_fields(body.credentials, label="MCP credentials") if body.credentials is not None else None
        )
        _validate_mcp_credentials(body.auth_type, credentials)
        _validate_mcp_allowlist(body)
        return await run_in_threadpool(
            store.upsert_mcp_server,
            tenant_id,
            actor,
            resource_id=None,
            name=body.name.strip(),
            endpoint=body.endpoint,
            auth_type=body.auth_type,
            credentials=credentials,
            allowed_tools=body.allowed_tools,
            allowed_resources=body.allowed_resources,
        )
    except ValueError as exc:
        raise _not_found_or_bad_input(exc) from None
    except Exception as exc:
        raise HTTPException(status_code=503, detail="MCP server could not be saved") from exc


def _validate_mcp_credentials(
    auth_type: str, credentials: dict[str, str] | None, *, allow_missing: bool = False
) -> None:
    if auth_type == "none":
        if credentials is not None:
            raise ValueError("Credentials are not allowed with auth_type=none")
        return
    if credentials is None and allow_missing:
        return
    if not credentials:
        raise ValueError("MCP authentication credentials are required")
    required = {"bearer": ("token", "access_token"), "api_key": ("api_key",)}[auth_type]
    allowed = {"bearer": {"token", "access_token"}, "api_key": {"api_key"}}[auth_type]
    if set(credentials) - allowed or not any(credentials.get(field) for field in required):
        raise ValueError(f"MCP {auth_type} credentials are invalid")


def _validate_mcp_allowlist(body: McpServerInput) -> None:
    if any(not value.strip() or len(value) > 160 for value in body.allowed_tools):
        raise ValueError("MCP tool identifiers must be 1 to 160 characters")
    if any(not value.strip() or len(value) > 500 for value in body.allowed_resources):
        raise ValueError("MCP resource identifiers must be 1 to 500 characters")


@router.put("/mcp/{server_id}")
async def update_mcp_server(
    server_id: str,
    request: Request,
    user: CurrentUser = Depends(ADMIN_ONLY),  # noqa: B008
):
    body = await _secret_input(request, McpServerInput)
    tenant_id, actor = _admin_identity(user)
    try:
        if body.auth_type == "none" and body.credentials is not None:
            raise ValueError("Credentials are not allowed with auth_type=none")
        credentials = (
            secret_fields(body.credentials, label="MCP credentials") if body.credentials is not None else None
        )
        existing = None
        if body.auth_type != "none" and credentials is None:
            current = await run_in_threadpool(store.list_mcp_servers, tenant_id)
            existing = next((server for server in current if server["id"] == server_id), None)
            if not existing or existing["auth_type"] != body.auth_type:
                raise ValueError("Credentials are required when changing MCP authentication")
        _validate_mcp_credentials(body.auth_type, credentials, allow_missing=True)
        _validate_mcp_allowlist(body)
        return await run_in_threadpool(
            store.upsert_mcp_server,
            tenant_id,
            actor,
            resource_id=server_id,
            name=body.name.strip(),
            endpoint=body.endpoint,
            auth_type=body.auth_type,
            credentials=credentials,
            allowed_tools=body.allowed_tools,
            allowed_resources=body.allowed_resources,
        )
    except ValueError as exc:
        raise _not_found_or_bad_input(exc) from None
    except Exception as exc:
        raise HTTPException(status_code=503, detail="MCP server could not be updated") from exc


@router.post("/mcp/{server_id}/approval")
async def approve_mcp_server(
    server_id: str,
    body: dict,
    user: CurrentUser = Depends(ADMIN_ONLY),  # noqa: B008
):
    if not isinstance(body, dict) or set(body) != {"approved"} or not isinstance(body["approved"], bool):
        raise HTTPException(status_code=422, detail="Approval requires a boolean approved field")
    tenant_id, actor = _admin_identity(user)
    try:
        await run_in_threadpool(
            store.set_mcp_approval, tenant_id, actor, server_id, approved=body["approved"]
        )
        return {"approved": body["approved"], "runtime_enabled": False}
    except ValueError as exc:
        raise _not_found_or_bad_input(exc) from None


@router.delete("/mcp/{server_id}")
async def remove_mcp_server(server_id: str, user: CurrentUser = Depends(ADMIN_ONLY)):  # noqa: B008
    tenant_id, actor = _admin_identity(user)
    try:
        await run_in_threadpool(store.delete_mcp_server, tenant_id, actor, server_id)
        return {"removed": True}
    except ValueError as exc:
        raise _not_found_or_bad_input(exc) from None


@router.put("/ai-policy")
async def update_ai_policy(request: Request, user: CurrentUser = Depends(ADMIN_ONLY)):  # noqa: B008
    body = await _secret_input(request, ModelPolicyInput)
    tenant_id, actor = _admin_identity(user)
    current = _public_model_policy(tenant_id)
    provider = body.provider or current.get("provider")
    if provider not in {"openai", "anthropic", "google", "compatible"}:
        raise HTTPException(status_code=422, detail="Configure an AI provider before model policy")
    if provider == "compatible":
        try:
            base_url = validate_public_https_url(
                body.base_url or current.get("base_url") or "",
                label="Compatible provider base URL",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    else:
        if body.base_url:
            raise HTTPException(status_code=422, detail="Only compatible providers accept a base URL")
        base_url = None
    if (body.input_price_per_million_usd is None) != (body.output_price_per_million_usd is None):
        raise HTTPException(status_code=422, detail="Set both token prices or leave both blank")
    if body.api_key is not None and not body.api_key.strip():
        raise HTTPException(status_code=422, detail="API key must not be empty")
    allowed = set(body.allowed_models)
    if body.model.strip() in allowed or (body.fallback_model and body.fallback_model not in allowed):
        raise HTTPException(
            status_code=422,
            detail="Models must be unique and include the fallback in allowed_models",
        )
    try:
        from creditpaper.auth.security.encryption import encrypt_envelope

        with tenant_cursor(tenant_id) as cur:
            cur.execute(
                "SELECT provider, encrypted_api_key FROM tenant_llm_providers WHERE tenant_id=%s FOR UPDATE",
                (tenant_id,),
            )
            existing = cur.fetchone()
            if provider != (existing[0] if existing else None) and not body.api_key:
                raise ValueError("Enter an API key when switching providers")
            if body.api_key:
                encrypted_key = encrypt_envelope(
                    {"api_key": body.api_key.strip()},
                    tenant_id=tenant_id,
                    record_id="tenant_llm_provider",
                    purpose="provider_api_key",
                )
            elif existing:
                encrypted_key = str(existing[1])
            else:
                raise ValueError("Configure a provider API key first")
            cur.execute(
                """INSERT INTO tenant_llm_providers
                       (tenant_id, provider, model, encrypted_api_key, base_url,
                        fallback_model, allowed_models, daily_token_limit,
                        monthly_cost_limit_usd, max_output_tokens,
                        input_price_per_million_usd, output_price_per_million_usd, enabled, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,now())
                   ON CONFLICT (tenant_id) DO UPDATE SET provider=EXCLUDED.provider,
                     model=EXCLUDED.model, encrypted_api_key=EXCLUDED.encrypted_api_key,
                     base_url=EXCLUDED.base_url, fallback_model=EXCLUDED.fallback_model,
                     allowed_models=EXCLUDED.allowed_models, daily_token_limit=EXCLUDED.daily_token_limit,
                     monthly_cost_limit_usd=EXCLUDED.monthly_cost_limit_usd,
                     max_output_tokens=EXCLUDED.max_output_tokens,
                     input_price_per_million_usd=EXCLUDED.input_price_per_million_usd,
                     output_price_per_million_usd=EXCLUDED.output_price_per_million_usd,
                     enabled=true, updated_at=now()""",
                (
                    tenant_id,
                    provider,
                    body.model.strip(),
                    encrypted_key,
                    base_url,
                    body.fallback_model,
                    sorted(allowed),
                    body.daily_token_limit,
                    body.monthly_cost_limit_usd,
                    body.max_output_tokens,
                    body.input_price_per_million_usd,
                    body.output_price_per_million_usd,
                ),
            )
            cur.execute(
                """INSERT INTO tenant_integration_audit_events
                       (tenant_id,actor_id,resource_type,action,details)
                   VALUES (%s,%s,'ai_gateway','ai_policy.updated',%s::jsonb)""",
                (
                    tenant_id,
                    actor,
                    json.dumps(
                        {"provider": provider, "model": body.model, "fallback_model": body.fallback_model}
                    ),
                ),
            )
        return _public_model_policy(tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=503, detail="AI model policy could not be saved") from exc


@router.post("/webhooks/{integration_id}")
async def integration_webhook(integration_id: str, request: Request):
    tenant_id = request.headers.get("x-creditpaper-tenant", "")
    try:
        tenant_id = str(UUID(tenant_id))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=401, detail="Webhook authentication failed") from None
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(status_code=413, detail="Webhook payload is too large")
    event_id = request.headers.get("x-integration-event-id", "")
    timestamp = request.headers.get("x-integration-timestamp", "")
    signature = request.headers.get("x-integration-signature", "")
    secret = await run_in_threadpool(store.webhook_secret, tenant_id, integration_id)
    if not verify_webhook_signature(
        secret or "", tenant_id, integration_id, timestamp, event_id, body, signature
    ):
        raise HTTPException(status_code=401, detail="Webhook authentication failed")
    try:
        json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Webhook payload must be JSON") from None
    try:
        accepted = store.record_webhook_event(
            tenant_id, integration_id, event_id, payload_digest=hashlib.sha256(body).hexdigest()
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Webhook event could not be recorded") from exc
    if not accepted:
        raise HTTPException(
            status_code=409,
            detail="Webhook event was already accepted or integration is unavailable",
        )
    return JSONResponse({"accepted": True})


@router.get("/service-contract")
async def service_contract(request: Request):
    claims = verify_service_request(request, audience="creditpaper-ai-gateway")
    return {
        "contract_version": "1.0",
        "service": "creditpaper-ai-gateway",
        "tenant_id": claims["tenant_id"],
        "capabilities": ["llm.complete", "integrations.invoke"],
        "mcp_runtime": "control_plane_only",
        "contracts": {
            "llm.complete": {
                "request": {"model": "string?", "messages": "array", "max_tokens": "integer?"},
                "response": {"text": "string", "model": "string", "provider": "string", "usage": "object"},
            },
            "integrations.invoke": {
                "request": {
                    "integration_id": "uuid",
                    "method": "GET|POST",
                    "path": "relative path",
                    "json": "object?",
                },
                "response": {"status_code": "integer", "json": "object?"},
            },
        },
    }


@router.post("/service-contract/llm.complete")
async def service_llm_complete(request: Request):
    claims = verify_service_request(request, audience="creditpaper-ai-gateway")
    tenant_id = claims["tenant_id"]
    try:
        raw_body = await request.body()
        if len(raw_body) > 1_000_000:
            raise ValueError("AI completion request body is too large")
        body = json.loads(raw_body)
        if not isinstance(body, dict) or set(body) - {"model", "messages", "max_tokens"}:
            raise ValueError("Invalid AI completion request")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > 100:
            raise ValueError("messages must be an array with 1 to 100 items")
        max_tokens = body.get("max_tokens", 4096)
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= 100000:
            raise ValueError("max_tokens is out of range")
        if any(
            not isinstance(m, dict)
            or m.get("role") not in {"user", "assistant"}
            or not isinstance(m.get("content"), str)
            for m in messages
        ):
            raise ValueError("Each message must contain a user/assistant role and text content")
        if any(set(message) - {"role", "content"} for message in messages):
            raise ValueError("Unsupported message fields")
        requested_model = body.get("model")
        if requested_model is not None and (
            not isinstance(requested_model, str) or not requested_model.strip()
        ):
            raise ValueError("model must be a non-empty string")
        from creditpaper.llm.client import messages_create, provider_request_scope
        from creditpaper.llm.tenant_config import active_provider_config, provider_session
        from creditpaper.obs.ledger import tenant_usage_session

        with provider_session(tenant_id):
            policy = _public_model_policy(tenant_id)
            if not policy.get("configured"):
                raise ValueError("Organization AI provider is not configured")
            selected_model = requested_model or policy["model"]
            allowed = set(policy.get("allowed_models") or [])
            if selected_model != policy["model"] and selected_model not in allowed:
                raise ValueError("Requested model is not approved by this organization")
            fallback = policy.get("fallback_model") if selected_model == policy["model"] else None
            if fallback not in allowed:
                fallback = None
            if max_tokens > int(policy.get("max_output_tokens") or 8192):
                raise ValueError("Requested output exceeds the organization model policy")
            predicted = (sum(len(message["content"]) for message in messages) + 3) // 4 + max_tokens
            daily_limit = policy.get("daily_token_limit")
            if daily_limit is not None and int(policy["daily_tokens_used"]) + predicted > int(daily_limit):
                raise ValueError("Organization daily token limit would be exceeded")
            monthly_limit = policy.get("monthly_cost_limit_usd")
            if monthly_limit is not None:
                from creditpaper.obs.ledger import prices_for

                configured_input_price = policy.get("input_price_per_million_usd")
                configured_output_price = policy.get("output_price_per_million_usd")
                prices = prices_for(selected_model)
                input_price = (
                    configured_input_price
                    if configured_input_price is not None
                    else (prices[0] if prices else None)
                )
                output_price = (
                    configured_output_price
                    if configured_output_price is not None
                    else (prices[1] if prices else None)
                )
                if input_price is None or output_price is None:
                    raise ValueError("Monthly cost limits require configured pricing for the selected model")
                projected_cost = predicted * max(float(input_price), float(output_price)) / 1_000_000
                if float(policy["monthly_cost_usd"]) + projected_cost > float(monthly_limit):
                    raise ValueError("Organization monthly cost limit would be exceeded")
            config = active_provider_config()
            candidates = [selected_model] + ([fallback] if fallback else [])
            result = None
            used_model = selected_model
            for candidate in candidates:
                try:
                    if config:
                        with (
                            provider_request_scope(replace(config, model=candidate)),
                            tenant_usage_session(tenant_id),
                        ):
                            result = messages_create(
                                tool="gateway:service",
                                model=candidate,
                                max_tokens=max_tokens,
                                messages=messages,
                            )
                    else:
                        with tenant_usage_session(tenant_id):
                            result = messages_create(
                                tool="gateway:service",
                                model=candidate,
                                max_tokens=max_tokens,
                                messages=messages,
                            )
                    used_model = candidate
                    break
                except Exception:
                    if candidate == candidates[-1]:
                        raise
            if result is None:
                raise RuntimeError("AI provider returned no result")
        return {
            "text": result.text,
            "model": used_model,
            "fallback_model": used_model != selected_model,
            "provider": config.provider if config else policy["provider"],
            "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens},
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="AI gateway request failed") from exc


@router.post("/service-contract/integrations.invoke")
async def service_integration_invoke(request: Request):
    claims = verify_service_request(request, audience="creditpaper-ai-gateway")
    tenant_id = claims["tenant_id"]
    integration_id = None
    attempts = 0
    try:
        raw_body = await request.body()
        if len(raw_body) > 1_000_000:
            raise ValueError("Integration request body is too large")
        body = json.loads(raw_body)
        if not isinstance(body, dict) or set(body) - {"integration_id", "method", "path", "json"}:
            raise ValueError("Invalid integration invocation request")
        integration_id, method = body.get("integration_id"), body.get("method", "GET")
        if not isinstance(integration_id, str) or method not in {"GET", "POST"}:
            raise ValueError("Invalid integration id or method")
        runtime = await run_in_threadpool(store.authorize_integration_call, tenant_id, integration_id)
        if not runtime:
            raise ValueError("Integration not found or disabled")
        if runtime.get("rate_limited"):
            raise HTTPException(status_code=429, detail="Integration rate limit reached")
        path = body.get("path", "")
        if not isinstance(path, str) or path.startswith(("//", "http://", "https://")) or "\\" in path:
            raise ValueError("Integration path must be relative")
        if any(part == ".." for part in unquote(path).split("/")):
            raise ValueError("Integration path cannot traverse directories")
        import httpx

        credentials = runtime["credentials"]
        headers: dict[str, str] = {}
        secret = credentials.get("api_key") or credentials.get("token") or credentials.get("access_token")
        if runtime["auth_type"] == "webhook":
            raise ValueError("Inbound webhook integrations cannot be invoked outbound")
        if runtime["auth_type"] == "api_key":
            if not isinstance(secret, str) or not secret:
                raise ValueError("Integration credentials are invalid")
            header_name = runtime["config"].get("api_key_header", "X-API-Key")
            if header_name.lower() not in {"authorization", "x-api-key"}:
                raise ValueError("Only standard API key headers are allowed")
            headers[header_name] = secret
        elif runtime["auth_type"] == "oauth_client_credentials":
            token_url = runtime["config"].get("token_url")
            validate_public_https_url(token_url, label="OAuth token URL")
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=15) as token_client:
                response = await token_client.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": credentials["client_id"],
                        "client_secret": credentials["client_secret"],
                        "scope": runtime["config"].get("scope", ""),
                    },
                )
            token = response.json().get("access_token") if response.is_success else None
            if not isinstance(token, str) or not token:
                raise HTTPException(status_code=502, detail="Integration OAuth authorization failed")
            headers["Authorization"] = f"Bearer {token}"
        else:
            if not isinstance(secret, str) or not secret:
                raise ValueError("Integration credentials are invalid")
            headers["Authorization"] = f"Bearer {secret}"

        validate_public_https_url(runtime["base_url"], label="Integration base URL")
        url = urljoin(runtime["base_url"] + "/", path.lstrip("/"))
        base, target = urlsplit(runtime["base_url"]), urlsplit(url)
        prefix = base.path.rstrip("/") + "/"
        if target.scheme != base.scheme or target.netloc != base.netloc or not target.path.startswith(prefix):
            raise ValueError("Integration path escaped the configured base URL")
        payload = body.get("json")
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("json must be an object")
        response = None
        max_attempts = 3 if method == "GET" else 1
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=30) as client:
            for attempt in range(max_attempts):
                attempts = attempt + 1
                try:
                    response = await client.request(method, url, headers=headers, json=payload)
                except httpx.TransportError:
                    if attempts == max_attempts:
                        raise
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                if response.status_code not in {429, 500, 502, 503, 504} or attempts == max_attempts:
                    break
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = min(2.0, max(0.0, float(retry_after)))
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(retry_after)
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=UTC)
                        delay = min(2.0, max(0.0, (retry_at - datetime.now(UTC)).total_seconds()))
                    except (TypeError, ValueError, OverflowError):
                        delay = 0.2 * (2**attempt)
                await asyncio.sleep(delay)
        if response is None:
            raise RuntimeError("Integration provider did not return a response")
        try:
            data = response.json()
        except ValueError:
            data = None
        store.record_integration_call(
            tenant_id,
            claims["sub"],
            integration_id,
            status="success" if response.is_success else "error",
            attempts=attempts,
        )
        if not response.is_success:
            raise HTTPException(status_code=502, detail="Integration provider rejected the request")
        return {"status_code": response.status_code, "json": data if isinstance(data, (dict, list)) else None}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except HTTPException:
        raise
    except Exception as exc:
        if isinstance(integration_id, str):
            try:
                store.record_integration_call(
                    tenant_id, claims["sub"], integration_id, status="transport_error", attempts=attempts
                )
            except Exception:
                pass
        raise HTTPException(status_code=502, detail="Integration provider request failed") from exc
