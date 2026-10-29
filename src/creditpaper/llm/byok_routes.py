"""Organization BYOK settings API. Secret fields are write-only."""

from __future__ import annotations

from decimal import Decimal
from ipaddress import ip_address
from socket import SOCK_STREAM, getaddrinfo
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from creditpaper.auth.dependencies import ADMIN_ONLY, CurrentUser
from creditpaper.llm import tenant_config

router = APIRouter(prefix="/llm/provider", tags=["llm-provider"])


class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "anthropic", "google", "compatible"]
    model: str = Field(min_length=1, max_length=160)
    api_key: str = Field(default="", max_length=4096)
    base_url: str | None = Field(default=None, max_length=500)
    input_price_per_million_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6)
    output_price_per_million_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6)

    @field_validator("model")
    @classmethod
    def clean_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Model is required")
        return value

    @field_validator("api_key")
    @classmethod
    def clean_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ValueError("Compatible provider URL must be an HTTPS origin or API base URL")
        if parsed.query or parsed.fragment:
            raise ValueError("Compatible provider URL cannot include a query or fragment")
        try:
            addresses = {
                ip_address(item[4][0])
                for item in getaddrinfo(parsed.hostname, parsed.port or 443, type=SOCK_STREAM)
            }
        except (OSError, ValueError):
            raise ValueError("Compatible provider host could not be resolved") from None
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError("Compatible provider host must resolve only to public IP addresses")
        return value

    @model_validator(mode="after")
    def validate_provider_fields(self):
        if self.provider == "compatible" and not self.base_url:
            raise ValueError("A compatible provider requires an HTTPS base URL")
        if self.provider != "compatible" and self.base_url:
            raise ValueError("Base URL is only supported for compatible providers")
        if (self.input_price_per_million_usd is None) != (self.output_price_per_million_usd is None):
            raise ValueError("Set both custom token prices, or leave both blank")
        return self


def _tenant(user: CurrentUser) -> str:
    if not user.tenant_id:
        raise HTTPException(status_code=403, detail="Organisation membership required")
    return str(user.tenant_id)


@router.get("")
async def get_provider_settings(user: CurrentUser = Depends(ADMIN_ONLY)):  # noqa: B008
    try:
        return await run_in_threadpool(tenant_config.provider_settings, _tenant(user))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Provider settings are temporarily unavailable") from exc


@router.put("")
async def put_provider_settings(
    request: Request,
    user: CurrentUser = Depends(ADMIN_ONLY),  # noqa: B008
):
    # Parse explicitly and suppress Pydantic's default ``input`` field in
    # validation errors: that object includes the submitted API key.
    try:
        incoming = await request.json()
        body = ProviderUpdate.model_validate(incoming)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid provider settings") from None
    try:
        return await run_in_threadpool(
            tenant_config.save_provider_settings,
            _tenant(user),
            provider=body.provider,
            model=body.model,
            api_key=body.api_key,
            base_url=body.base_url,
            input_price_per_million_usd=body.input_price_per_million_usd,
            output_price_per_million_usd=body.output_price_per_million_usd,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Provider settings could not be saved") from exc


@router.delete("")
async def delete_provider_settings(user: CurrentUser = Depends(ADMIN_ONLY)):  # noqa: B008
    try:
        await run_in_threadpool(tenant_config.remove_provider_settings, _tenant(user))
        return {"removed": True}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Provider settings could not be removed") from exc
