from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from creditpaper.onboarding.services.website_validator import (
    resolve_and_check_host,
    validate_redirect_url,
)

DEFAULT_TIMEOUT = float(os.getenv("WEBSITE_REQUEST_TIMEOUT", "12"))
DEFAULT_MAX_SIZE = int(os.getenv("WEBSITE_MAX_RESPONSE_SIZE", str(2 * 1024 * 1024)))
DEFAULT_MAX_REDIRECTS = int(os.getenv("WEBSITE_MAX_REDIRECTS", "5"))
ALLOWED_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    html: str
    duration_ms: int


class WebsiteFetchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def fetch_website(url: str) -> FetchResult:
    import time
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    block = resolve_and_check_host(host)
    if block:
        friendly = (
            "This website address is not allowed for security reasons."
            if block == "WEBSITE_BLOCKED"
            else "We couldn't reach this website."
        )
        raise WebsiteFetchError(block, friendly)  # noqa: B904

    start = time.monotonic()
    timeout = httpx.Timeout(DEFAULT_TIMEOUT, connect=5.0)
    limits = httpx.Limits(max_connections=5)

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            headers={"User-Agent": "CreditPaper/1.0 onboarding bot"},
        ) as client:
            current_url = url
            for _ in range(DEFAULT_MAX_REDIRECTS + 1):
                resp = await client.get(current_url)
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location") or resp.headers.get("Location")
                    if not loc:
                        raise WebsiteFetchError(  # noqa: B904
                            "WEBSITE_UNREACHABLE",
                            "We couldn't reach this website. Check the URL and try again.",
                        )
                    if loc.startswith("/"):
                        p2 = urlparse(current_url)
                        loc = f"{p2.scheme}://{p2.netloc}{loc}"
                    if not validate_redirect_url(loc):
                        raise WebsiteFetchError(  # noqa: B904
                            "WEBSITE_BLOCKED", "This website address is not allowed for security reasons."
                        )
                    rh = urlparse(loc).hostname or ""
                    b2 = resolve_and_check_host(rh)
                    if b2:
                        friendly = (
                            "This website address is not allowed for security reasons."
                            if b2 == "WEBSITE_BLOCKED"
                            else "We couldn't reach this website."
                        )
                        raise WebsiteFetchError(b2, friendly)  # noqa: B904
                    current_url = loc
                    continue

                if resp.status_code in (401, 403, 429, 451, 406):
                    msg = {
                        401: "This website requires authentication and does not permit automated access.",
                        403: "This website does not permit automated scraping.",
                        429: "This website is rate-limiting requests. Please try again later.",
                        451: "This website is unavailable for legal reasons and cannot be scraped.",
                        406: "This website does not permit automated scraping.",
                    }.get(resp.status_code, "This website does not permit automated scraping.")
                    raise WebsiteFetchError("WEBSITE_SCRAPING_NOT_ALLOWED", msg)  # noqa: B904

                if resp.status_code in (402, 423, 451):
                    raise WebsiteFetchError(  # noqa: B904
                        "WEBSITE_SCRAPING_NOT_ALLOWED",
                        "This website does not permit automated scraping.",
                    )

                if resp.status_code >= 400:
                    if resp.status_code in (403, 401):
                        raise WebsiteFetchError(  # noqa: B904
                            "WEBSITE_SCRAPING_NOT_ALLOWED",
                            "This website does not permit automated scraping.",
                        )
                    raise WebsiteFetchError("WEBSITE_UNREACHABLE", f"HTTP {resp.status_code}")  # noqa: B904

                ct = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ct and ct not in ALLOWED_CONTENT_TYPES:
                    if "text/html" not in ct and "xhtml" not in ct:
                        if "application/json" in ct or "text/plain" in ct:
                            raise WebsiteFetchError(  # noqa: B904
                                "WEBSITE_SCRAPING_NOT_ALLOWED",
                                "This website does not permit automated scraping or returned an unsupported format.",  # noqa: E501
                            )
                        raise WebsiteFetchError(  # noqa: B904
                            "WEBSITE_CONTENT_UNSUPPORTED", f"unsupported content-type: {ct}"
                        )

                content_length = resp.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > DEFAULT_MAX_SIZE:
                            raise WebsiteFetchError(  # noqa: B904
                                "WEBSITE_CONTENT_UNSUPPORTED",
                                "This website's content is too large to process.",
                            )
                    except ValueError:
                        pass

                raw = resp.content
                if len(raw) > DEFAULT_MAX_SIZE:
                    raise WebsiteFetchError(  # noqa: B904
                        "WEBSITE_CONTENT_UNSUPPORTED", "This website's content is too large to process."
                    )

                if len(raw) < 500:
                    raise WebsiteFetchError(  # noqa: B904
                        "WEBSITE_CONTENT_UNSUPPORTED", "This website returned too little content to analyze."
                    )

                try:
                    html = raw.decode("utf-8", errors="replace")
                except Exception:
                    html = raw.decode("latin-1", errors="replace")

                low = html.lower()
                blocked_markers = [
                    "cf-challenge",
                    "cloudflare",
                    "attention required",
                    "access denied",
                    "403 forbidden",
                    "please enable javascript",
                    "captcha",
                    "unavailable for legal reasons",
                    "you have been blocked",
                ]
                if any(m in low for m in blocked_markers) and len(html) < 20000:
                    raise WebsiteFetchError(  # noqa: B904
                        "WEBSITE_SCRAPING_NOT_ALLOWED",
                        "This website does not permit automated scraping.",
                    )

                if (
                    "x-frame-options" in (resp.headers.get("x-frame-options") or "").lower()
                    and len(html.strip()) < 1000
                ):
                    pass

                html = html[:500_000]

                duration_ms = int((time.monotonic() - start) * 1000)
                return FetchResult(
                    url=url,
                    final_url=current_url,
                    status_code=resp.status_code,
                    html=html,
                    duration_ms=duration_ms,
                )

            raise WebsiteFetchError(  # noqa: B904
                "WEBSITE_UNREACHABLE", "We couldn't reach this website. Check the URL and try again."
            )

    except WebsiteFetchError:
        raise
    except httpx.TimeoutException:
        raise WebsiteFetchError(  # noqa: B904
            "WEBSITE_TIMEOUT", "This website took too long to respond. Please try again."
        )
    except httpx.ConnectError:
        raise WebsiteFetchError(  # noqa: B904
            "WEBSITE_UNREACHABLE", "We couldn't reach this website. Check the URL and try again."
        )
    except OSError:
        raise WebsiteFetchError(  # noqa: B904
            "WEBSITE_UNREACHABLE", "We couldn't reach this website. Check the URL and try again."
        )
