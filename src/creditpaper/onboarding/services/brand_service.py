from __future__ import annotations

import asyncio

from creditpaper.onboarding.services.brand_extractor import extract_brand
from creditpaper.onboarding.services.brand_validator import validate_brand_result
from creditpaper.onboarding.services.website_fetcher import WebsiteFetchError, fetch_website
from creditpaper.onboarding.services.website_validator import validate_website_url
from creditpaper.onboarding.utils.store import save_brand_profile


async def analyze_website(tenant_id: str, website_url: str) -> dict:
    normalized, err = validate_website_url(website_url)
    if err:
        raise WebsiteFetchError(err, err)  # noqa: B904

    fetch_result = await fetch_website(normalized)

    raw = extract_brand(fetch_result.html, fetch_result.final_url, fetch_result.duration_ms)
    validated = validate_brand_result(raw)
    validated["website"] = fetch_result.final_url

    await asyncio.to_thread(save_brand_profile, tenant_id, validated)
    return validated
