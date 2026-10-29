"""Brand colours entered by hand, laid over whatever was already read from the website."""

from __future__ import annotations

from typing import Any

DEFAULT_VOCABULARY = ["facility", "sponsor", "security", "drawdown"]


def site_key(url: str | None) -> str:
    """The comparable part of a website address: host and path, without scheme, www or trailing slash."""
    s = (url or "").strip().lower()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
    if s.startswith("www."):
        s = s[4:]
    return s.rstrip("/")


def manual_brand_profile(
    existing: dict[str, Any] | None,
    colors: dict[str, str | None],
    website_url: str | None,
    name: str | None,
    default_typography: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Correcting colours keeps the logo and typefaces already read from the same website.

    A different website starts again from defaults: a logo read from one site never sits beside
    colours typed for another. No website given means the saved one stands.

    With no typefaces read from the site, `default_typography` (the default paper theme's
    fonts, passed by the caller) is recorded; without it, none is recorded and the paper
    theme resolver applies the platform default.
    """
    website = (website_url or "").strip()[:500]
    previous_site = (existing or {}).get("website") or ""
    same_site = existing is not None and (not website or site_key(website) == site_key(previous_site))
    kept: dict[str, Any] = ((existing or {}).get("brand") or {}) if same_site else {}

    return {
        "status": "completed" if colors.get("secondary") and colors.get("accent") else "partial",
        "website": previous_site if same_site and not website else website,
        "duration_ms": 0,
        "brand": {
            "name": (name or kept.get("name") or "Your Firm")[:80],
            "logo": kept.get("logo"),
            "colors": colors,
            "typography": kept.get("typography") or default_typography,
            "vocabulary": kept.get("vocabulary") or DEFAULT_VOCABULARY,
        },
    }
