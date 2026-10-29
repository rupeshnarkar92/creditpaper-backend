"""Paper theme resolution (spec 2026-09-15 §4).

A theme is the firm's fonts, brand colours and logo packaged with the standard
typography scale and page setup. The rules run on the server only:

  - colours: `heading` is the brand primary, `accent` the brand accent (or secondary),
    each darkened in OKLCH lightness if under 4.5:1 / 3:1 on white; `accent_tint` is
    derived from the accent; `text`, `muted`, `rule` always come from the default theme.
  - fonts: scraped names matched against the self-hosted set; no match -> default by
    category.
  - logo: served through the API from the stored brand, never hot-linked.
  - typography and page: always from the default theme (MVP).
  - brand missing or malformed: the default theme, `source: "platform_default"`. The
    resolver never fails a request because of brand data.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from creditpaper.paper import colour, fonts
from creditpaper.paper.defaults_store import load_current

log = logging.getLogger(__name__)


def _default_colours(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("colours") or {}
    out: dict[str, str] = {}
    for key in ("heading", "accent", "text", "muted", "rule"):
        hexed = colour.normalise_hex(raw.get(key))
        if hexed is None:
            raise ValueError(f"default paper theme colour '{key}' is missing or invalid")
        out[key] = hexed
    return out


def _brand_block(brand_profile: object) -> dict[str, Any] | None:
    if not isinstance(brand_profile, dict):
        return None
    brand = brand_profile.get("brand")
    return brand if isinstance(brand, dict) else None


def _logo_source_url(brand: dict[str, Any] | None) -> str | None:
    if not brand:
        return None
    logo = brand.get("logo")
    if not isinstance(logo, dict):
        return None
    url = logo.get("url")
    if isinstance(url, str) and url.strip().lower().startswith(("https://", "http://")):
        return url.strip()
    return None


def brand_logo_url(brand_profile: object) -> str | None:
    """The stored logo URL the logo endpoint may fetch, or None."""
    return _logo_source_url(_brand_block(brand_profile))


def _theme_from(
    *,
    source: str,
    version: str,
    firm_name: str | None,
    logo_url: str | None,
    heading_font: fonts.FontEntry,
    body_font: fonts.FontEntry,
    colours: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    heading = colour.ensure_contrast(colours["heading"], colour.HEADING_MIN_CONTRAST)
    accent = colour.ensure_contrast(colours["accent"], colour.ACCENT_MIN_CONTRAST)
    return {
        "source": source,
        "version": version,
        "firm_name": firm_name,
        "logo": {"url": logo_url, "alt": firm_name or ""} if logo_url else None,
        "fonts": {"heading": heading_font.as_theme(), "body": body_font.as_theme()},
        "colours": {
            "heading": heading,
            "accent": accent,
            "accent_tint": colour.tint(accent),
            "text": colours["text"],
            "muted": colours["muted"],
            "rule": colours["rule"],
        },
        "typography": copy.deepcopy(payload.get("typography") or {}),
        "page": copy.deepcopy(payload.get("page") or {}),
    }


def resolve_theme(
    brand_profile: object,
    default_record: dict[str, Any],
    *,
    logo_endpoint: str | None,
    firm_name_fallback: str | None = None,
) -> dict[str, Any]:
    """Pure: a brand profile plus the current default `paper_theme` record -> theme.

    `logo_endpoint` is the API path the browser should load the logo from; it is
    only used when the brand carries a logo to serve.
    """
    payload = default_record.get("payload") or {}
    theme_version = int(default_record.get("version") or 0)
    defaults = _default_colours(payload)
    default_fonts = payload.get("fonts") or {}
    heading_default = str((default_fonts.get("heading") or {}).get("family") or "")
    body_default = str((default_fonts.get("body") or {}).get("family") or "")
    category_defaults = {
        k: str(v) for k, v in (payload.get("font_defaults") or {}).items() if isinstance(v, str)
    }
    category_defaults.setdefault("serif", heading_default)
    category_defaults.setdefault("sans", body_default)

    def platform_theme() -> dict[str, Any]:
        return _theme_from(
            source="platform_default",
            version=f"platform|theme:{theme_version}",
            firm_name=firm_name_fallback,
            logo_url=None,
            heading_font=fonts.resolve_font(
                None, role_default=heading_default, category_defaults=category_defaults
            ),
            body_font=fonts.resolve_font(
                None, role_default=body_default, category_defaults=category_defaults
            ),
            colours=defaults,
            payload=payload,
        )

    brand = _brand_block(brand_profile)
    if brand is None:
        return platform_theme()
    try:
        raw_colours = brand.get("colors")
        if not isinstance(raw_colours, dict):
            raw_colours = {}
        heading = colour.normalise_hex(raw_colours.get("primary")) or defaults["heading"]
        accent = (
            colour.normalise_hex(raw_colours.get("accent"))
            or colour.normalise_hex(raw_colours.get("secondary"))
            or defaults["accent"]
        )
        typography = brand.get("typography")
        if not isinstance(typography, dict):
            typography = {}
        heading_font = fonts.resolve_font(
            typography.get("heading"), role_default=heading_default, category_defaults=category_defaults
        )
        body_font = fonts.resolve_font(
            typography.get("body") or typography.get("heading"),
            role_default=body_default,
            category_defaults=category_defaults,
        )
        name = brand.get("name")
        firm_name = name.strip()[:80] if isinstance(name, str) and name.strip() else firm_name_fallback
        updated = brand_profile.get("updated_at") if isinstance(brand_profile, dict) else None
        logo_url = logo_endpoint if _logo_source_url(brand) and logo_endpoint else None
        return _theme_from(
            source="firm",
            version=f"brand:{updated or 'unknown'}|theme:{theme_version}",
            firm_name=firm_name,
            logo_url=logo_url,
            heading_font=heading_font,
            body_font=body_font,
            colours={**defaults, "heading": heading, "accent": accent},
            payload=payload,
        )
    except Exception:  # noqa: BLE001 — brand data never fails the request
        log.warning("paper theme: malformed brand profile, using the platform default")
        return platform_theme()


# --- DB-backed ----------------------------------------------------------------


def load_brand(tenant_id: str) -> dict[str, Any] | None:
    try:
        from creditpaper.onboarding.utils.store import load_brand_profile

        return load_brand_profile(tenant_id)
    except Exception:  # noqa: BLE001 — a brand read failure is treated as no brand
        log.warning("paper theme: brand profile unavailable for tenant")
        return None


def _firm_name(tenant_id: str) -> str | None:
    try:
        from creditpaper.db import cursor

        with cursor() as cur:
            cur.execute("SELECT company FROM domains WHERE id = %s", (str(tenant_id),))
            row = cur.fetchone()
        if row and row[0] and str(row[0]).strip():
            return str(row[0]).strip()[:80]
        return None
    except Exception:  # noqa: BLE001
        return None


def get_theme(tenant_id: str, *, logo_endpoint: str) -> dict[str, Any]:
    """The resolved theme for a tenant. Raises PlatformDefaultMissing only when the
    platform default itself is absent (a deployment fault)."""
    default_record = load_current("paper_theme")
    brand = load_brand(tenant_id)
    block = _brand_block(brand) or {}
    has_name = isinstance(block.get("name"), str) and bool(block["name"].strip())
    fallback_name = None if has_name else _firm_name(tenant_id)
    return resolve_theme(brand, default_record, logo_endpoint=logo_endpoint, firm_name_fallback=fallback_name)


def default_theme_colour(name: str) -> str | None:
    """A colour from the current default paper theme, or None if it cannot be read.

    For code paths that previously carried a hard-coded fallback colour and must not
    fail when the database is unreachable.
    """
    try:
        payload = load_current("paper_theme").get("payload") or {}
        return colour.normalise_hex((payload.get("colours") or {}).get(name))
    except Exception:  # noqa: BLE001
        return None


def default_theme_fonts() -> dict[str, str] | None:
    """`{heading, body}` family names from the current default paper theme, or None."""
    try:
        payload = load_current("paper_theme").get("payload") or {}
        f = payload.get("fonts") or {}
        heading = (f.get("heading") or {}).get("family")
        body = (f.get("body") or {}).get("family")
        if isinstance(heading, str) and isinstance(body, str):
            return {"heading": heading, "body": body}
    except Exception:  # noqa: BLE001
        pass
    return None
