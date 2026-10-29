from __future__ import annotations

import re

from creditpaper.paper.theme import default_theme_colour

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
FONT_RE = re.compile(r"^[a-zA-Z0-9\s-]{2,40}$")


def _sanitize_text(s: str, max_len: int = 80) -> str:
    s = re.sub(r"<[^>]*>", "", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s.strip()[:max_len]


def validate_brand_result(result: dict) -> dict:
    brand = result.get("brand", {})
    name = _sanitize_text(str(brand.get("name", "")), 80) or "Your Firm"

    colors = brand.get("colors", {})
    raw_primary = colors.get("primary")
    raw_secondary = colors.get("secondary")
    raw_accent = colors.get("accent")

    def _norm_hex(v: str | None, fallback: str | None) -> str | None:
        if v is None:
            return fallback
        s = str(v).strip()
        if not HEX_RE.match(s):
            return fallback
        return s.upper()

    # No usable primary: the default paper theme's heading colour (a stored platform
    # default), or None when that record cannot be read.
    primary = _norm_hex(raw_primary, None) or default_theme_colour("heading")
    secondary = _norm_hex(raw_secondary, None)
    accent = _norm_hex(raw_accent, None)

    typo = brand.get("typography", {})
    heading = _sanitize_text(str(typo.get("heading", "Inter")), 40) or "Inter"
    body = _sanitize_text(str(typo.get("body", "Inter")), 40) or heading
    if not FONT_RE.match(heading):
        heading = "Inter"
    if not FONT_RE.match(body):
        body = heading

    vocab_raw = brand.get("vocabulary", [])
    vocab: list[str] = []
    seen = set()
    for v in vocab_raw[:12]:
        vs = _sanitize_text(str(v), 30)
        if not vs or len(vs) < 2:
            continue
        low = vs.lower()
        if low in seen:
            continue
        seen.add(low)
        vocab.append(vs)
        if len(vocab) >= 8:
            break
    if not vocab:
        vocab = ["facility", "sponsor", "security", "drawdown"]

    logo = brand.get("logo")
    logo_out = None
    if isinstance(logo, dict):
        url = str(logo.get("url", "")).strip()[:500]
        if url.startswith("https://"):
            alt = _sanitize_text(str(logo.get("alt", name)), 80) or name
            logo_out = {"url": url, "alt": alt}

    status = result.get("status", "completed")
    if status not in ("completed", "partial", "failed"):
        status = "completed"
    if not logo_out and not vocab:
        status = "partial"

    return {
        "status": status,
        "website": str(result.get("website", ""))[:500],
        "duration_ms": int(result.get("duration_ms", 0)),
        "brand": {
            "name": name,
            "logo": logo_out,
            "colors": {"primary": primary, "secondary": secondary, "accent": accent},
            "typography": {"heading": heading, "body": body},
            "vocabulary": vocab,
        },
    }
