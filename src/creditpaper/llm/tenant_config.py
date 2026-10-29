"""Tenant-scoped BYOK settings and usage attribution.

The context carries ciphertext, never plaintext credentials. AES-GCM decryption is
performed only in the transport immediately before a provider request.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from creditpaper.auth.security.encryption import decrypt_envelope, encrypt_envelope
from creditpaper.db import tenant_cursor

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantProviderConfig:
    tenant_id: str
    provider: str
    model: str
    encrypted_api_key: str
    base_url: str | None = None
    input_price_per_million_usd: Decimal | None = None
    output_price_per_million_usd: Decimal | None = None


@dataclass(frozen=True)
class ProviderScope:
    tenant_id: str
    config: TenantProviderConfig | None


_SCOPE: ContextVar[ProviderScope | None] = ContextVar("creditpaper_provider_scope", default=None)


def active_provider_scope() -> ProviderScope | None:
    return _SCOPE.get()


def active_provider_config() -> TenantProviderConfig | None:
    scope = active_provider_scope()
    return scope.config if scope else None


def _tenant_uuid(tenant_id: str) -> str | None:
    try:
        return str(uuid.UUID(str(tenant_id)))
    except (ValueError, TypeError, AttributeError):
        # Unit tests and offline scripts use synthetic tenant labels. They do not
        # have a database-backed organization configuration.
        return None


def _load_config(tenant_id: str) -> TenantProviderConfig | None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """SELECT provider, model, encrypted_api_key, base_url,
                      input_price_per_million_usd, output_price_per_million_usd
                 FROM tenant_llm_providers WHERE tenant_id = %s AND enabled = true""",
            (tenant_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    encrypted_api_key = str(row[2])
    return TenantProviderConfig(
        tenant_id=tenant_id,
        provider=str(row[0]),
        model=str(row[1]),
        encrypted_api_key=encrypted_api_key,
        base_url=str(row[3]) if row[3] else None,
        input_price_per_million_usd=Decimal(row[4]) if row[4] is not None else None,
        output_price_per_million_usd=Decimal(row[5]) if row[5] is not None else None,
    )


@contextmanager
def provider_session(tenant_id: str | None) -> Iterator[None]:
    """Load one tenant's encrypted config and propagate it into child threads.

    The existing context is reused for nested calls for the same tenant, avoiding
    an extra database read in ingestion paths nested inside a worker.
    """
    from creditpaper.llm.client import provider_request_scope
    from creditpaper.obs.ledger import tenant_usage_session

    tid = _tenant_uuid(str(tenant_id)) if tenant_id else None
    if tid is None:
        token = _SCOPE.set(None)
        try:
            with provider_request_scope(None), tenant_usage_session(None):
                yield
        finally:
            _SCOPE.reset(token)
        return

    current = active_provider_scope()
    if current and current.tenant_id == tid:
        from creditpaper.obs.ledger import active_tenant_id, tenant_usage_session

        active_tenant = active_tenant_id()
        config = current.config
        with provider_request_scope(config):
            if active_tenant == tid:
                yield
            else:
                with tenant_usage_session(tid):
                    yield
        return

    scope = ProviderScope(tenant_id=tid, config=_load_config(tid))
    token = _SCOPE.set(scope)
    try:
        with provider_request_scope(scope.config):
            with tenant_usage_session(tid):
                yield
    finally:
        _SCOPE.reset(token)


def configured_model_policy(tenant_id: str) -> dict[str, Any]:
    """Return only non-secret model policy plus current period usage totals."""
    tid = _tenant_uuid(tenant_id)
    if tid is None:
        raise ValueError("Organisation identifier is invalid")
    with tenant_cursor(tid) as cur:
        cur.execute(
            """SELECT provider, model, fallback_model, allowed_models, daily_token_limit,
                      monthly_cost_limit_usd, max_output_tokens
                 FROM tenant_llm_providers WHERE tenant_id=%s AND enabled=true""",
            (tid,),
        )
        row = cur.fetchone()
        if not row:
            return {"configured": False}
        cur.execute(
            """SELECT coalesce(sum(input_tokens + output_tokens), 0)
                 FROM tenant_llm_usage_events
                WHERE tenant_id=%s AND created_at >= date_trunc('day', now())""",
            (tid,),
        )
        daily_tokens = int(cur.fetchone()[0])
        cur.execute(
            """SELECT coalesce(sum(cost_usd), 0)
                 FROM tenant_llm_usage_events
                WHERE tenant_id=%s AND created_at >= date_trunc('month', now())""",
            (tid,),
        )
        monthly_cost = float(cur.fetchone()[0])
    return {
        "configured": True,
        "provider": str(row[0]),
        "model": str(row[1]),
        "fallback_model": str(row[2]) if row[2] else None,
        "allowed_models": list(row[3] or []),
        "daily_token_limit": row[4],
        "monthly_cost_limit_usd": float(row[5]) if row[5] is not None else None,
        "max_output_tokens": int(row[6]),
        "daily_tokens_used": daily_tokens,
        "monthly_cost_usd": monthly_cost,
    }


def decrypt_api_key(config: TenantProviderConfig) -> str:
    """Decrypt just before a request; the plaintext is never stored in context."""
    from creditpaper.auth.security.encryption import decrypt

    if config.encrypted_api_key.startswith(("v2:", "v3:")):
        payload = decrypt_envelope(
            config.encrypted_api_key,
            tenant_id=config.tenant_id,
            record_id="tenant_llm_provider",
            purpose="provider_api_key",
        )
    else:
        payload = decrypt(config.encrypted_api_key)
    key = payload.get("api_key") if isinstance(payload, dict) else None
    if not isinstance(key, str) or not key:
        raise RuntimeError("Configured provider credential is invalid")
    return key


def _public_config_row(tenant_id: str) -> dict[str, Any] | None:
    with tenant_cursor(tenant_id) as cur:
        cur.execute(
            """SELECT provider, model, base_url, input_price_per_million_usd,
                      output_price_per_million_usd, updated_at
                 FROM tenant_llm_providers WHERE tenant_id = %s AND enabled = true""",
            (tenant_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "configured": True,
        "provider": str(row[0]),
        "model": str(row[1]),
        "base_url": str(row[2]) if row[2] else None,
        "input_price_per_million_usd": float(row[3]) if row[3] is not None else None,
        "output_price_per_million_usd": float(row[4]) if row[4] is not None else None,
        "updated_at": row[5].isoformat() if row[5] else None,
    }


def provider_settings(tenant_id: str) -> dict[str, Any]:
    """Safe settings and 30-day usage aggregates. Secret material is never selected."""
    tid = _tenant_uuid(tenant_id)
    if tid is None:
        raise ValueError("Organisation identifier is invalid")
    config = _public_config_row(tid)
    with tenant_cursor(tid) as cur:
        cur.execute(
            """SELECT provider, model, count(*) AS calls,
                      coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0),
                      coalesce(sum(cost_usd), 0), bool_or(unpriced), bool_or(failed)
                 FROM tenant_llm_usage_events
                WHERE tenant_id = %s AND created_at >= now() - interval '30 days'
                GROUP BY provider, model ORDER BY provider, model""",
            (tid,),
        )
        rows = cur.fetchall()
    usage = [
        {
            "provider": str(row[0]),
            "model": str(row[1]),
            "calls": int(row[2]),
            "input_tokens": int(row[3]),
            "output_tokens": int(row[4]),
            "cost_usd": float(row[5]),
            "has_unpriced": bool(row[6]),
            "has_failures": bool(row[7]),
        }
        for row in rows
    ]
    return {"provider": config or {"configured": False}, "usage_30d": usage}


def save_provider_settings(
    tenant_id: str,
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: str | None,
    input_price_per_million_usd: Decimal | None,
    output_price_per_million_usd: Decimal | None,
) -> dict[str, Any]:
    tid = _tenant_uuid(tenant_id)
    if tid is None:
        raise ValueError("Organisation identifier is invalid")
    model = model.strip()
    api_key = api_key.strip()
    base_url = base_url.strip().rstrip("/") if base_url else None

    with tenant_cursor(tid) as cur:
        cur.execute(
            "SELECT provider, encrypted_api_key FROM tenant_llm_providers WHERE tenant_id = %s FOR UPDATE",
            (tid,),
        )
        existing = cur.fetchone()
        if api_key:
            encrypted_api_key = encrypt_envelope(
                {"api_key": api_key},
                tenant_id=tid,
                record_id="tenant_llm_provider",
                purpose="provider_api_key",
            )
        elif existing and str(existing[0]) == provider:
            encrypted_api_key = str(existing[1])
        else:
            raise ValueError("Enter an API key for this provider")
        cur.execute(
            """INSERT INTO tenant_llm_providers
                   (tenant_id, provider, model, encrypted_api_key, base_url,
                    input_price_per_million_usd, output_price_per_million_usd, enabled, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, true, now())
               ON CONFLICT (tenant_id) DO UPDATE SET
                   provider = EXCLUDED.provider, model = EXCLUDED.model,
                   encrypted_api_key = EXCLUDED.encrypted_api_key, base_url = EXCLUDED.base_url,
                   input_price_per_million_usd = EXCLUDED.input_price_per_million_usd,
                   output_price_per_million_usd = EXCLUDED.output_price_per_million_usd,
                   enabled = true, updated_at = now()""",
            (
                tid,
                provider,
                model,
                encrypted_api_key,
                base_url,
                input_price_per_million_usd,
                output_price_per_million_usd,
            ),
        )
    return {"saved": True, **(_public_config_row(tid) or {})}


def remove_provider_settings(tenant_id: str) -> None:
    tid = _tenant_uuid(tenant_id)
    if tid is None:
        raise ValueError("Organisation identifier is invalid")
    with tenant_cursor(tid) as cur:
        cur.execute("DELETE FROM tenant_llm_providers WHERE tenant_id = %s", (tid,))


def record_usage(tenant_id: str, provider: str, record: Any) -> None:
    """Append token/cost telemetry; caller intentionally isolates errors from inference."""
    try:
        with tenant_cursor(tenant_id) as cur:
            cur.execute(
                """INSERT INTO tenant_llm_usage_events
                       (tenant_id, provider, model, tool, input_tokens, output_tokens,
                        cache_write_tokens, cache_read_tokens, cost_usd, latency_s,
                        unpriced, truncated, failed)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    tenant_id,
                    provider,
                    record.model,
                    record.tool,
                    record.input_tokens,
                    record.output_tokens,
                    record.cache_write_tokens,
                    record.cache_read_tokens,
                    record.cost_usd,
                    record.latency_s,
                    record.unpriced,
                    record.truncated,
                    record.failed,
                ),
            )
    except Exception as exc:  # noqa: BLE001 - analytics failure must not fail generation
        log.warning("tenant LLM usage write failed (%s)", type(exc).__name__)


def cost_for_provider(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    *,
    config: TenantProviderConfig | None = None,
) -> tuple[float, bool]:
    """Compute USD cost from tenant pricing when supplied, otherwise known platform rates."""
    from creditpaper.obs.ledger import cost_usd, prices_for

    config = config or active_provider_config()
    if (
        config
        and config.input_price_per_million_usd is not None
        and config.output_price_per_million_usd is not None
    ):
        input_price = float(config.input_price_per_million_usd)
        output_price = float(config.output_price_per_million_usd or 0)
        amount = (
            input_tokens * input_price
            + output_tokens * output_price
            + cache_write_tokens * input_price * 1.25
            + cache_read_tokens * input_price * 0.10
        ) / 1_000_000
        return amount, False
    price_known = prices_for(model) is not None
    return (
        cost_usd(model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens),
        not price_known,
    )
