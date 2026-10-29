"""The house style a tenant's paper is generated with (D10).

Section agents consume a `HouseStyleProfile`-shaped dict (`agents/section.py` reads
`client`, `paper_title`, `voice`, `section_playbooks`, `section_exemplars`). For a
tenant it is built from, in order:

  1. the tenant's learned `voice_profile` (onboarding, `voice_service.py`): register,
     tone, sentence length, terminology and rules;
  2. otherwise the current `platform_defaults` voice record.

Firm name: the brand profile's name, else the firm's company name, else "the lender".
Playbooks and exemplars stay empty: the voice profile carries tenant prose samples
(`samples.tenant_voice`), and those are shown to the analyst during onboarding, never
fed to a section agent as form reference. Structure phrases and learnings are not
mapped either: the heuristic fallback lifts them verbatim from the corpus, so they can
carry deal text.

No tenant path uses the 3Capital seed (`seed_3capital_profile` is for scripts and tests).
Pure: callers pass the records in.
"""

from __future__ import annotations

from typing import Any

from creditpaper.profiles.house_style import HouseStyleProfile

NEUTRAL_FIRM = "the lender"
PAPER_TITLE = "Credit Paper"
# Onboarding's manual brand form writes this when no name was given.
_PLACEHOLDER_NAMES = {"your firm"}
# Platform voice keys that are bookkeeping, not guidance for a writer.
_PLATFORM_META_KEYS = {"placeholder", "note"}

_MAX_VALUE = 200
_MAX_TERMS = 12
_MAX_TERM = 60
_MAX_RULES = 8


def _clean(value: object, limit: int = _MAX_VALUE) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] if text else None


def _clean_list(values: object, *, max_items: int, max_len: int) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for item in values:
        text = _clean(item, max_len)
        if text and text not in out:
            out.append(text)
        if len(out) >= max_items:
            break
    return out


def voice_from_profile(voice_profile: object) -> dict[str, str]:
    """The agent voice lines from a learned `voice_profile` row. Empty when it has none."""
    if not isinstance(voice_profile, dict):
        return {}
    style = voice_profile.get("style")
    style = style if isinstance(style, dict) else {}
    voice: dict[str, str] = {}
    for key in ("register", "tone", "sentence_length"):
        text = _clean(style.get(key))
        if text:
            voice[key] = text
    terms = _clean_list(voice_profile.get("terminology"), max_items=_MAX_TERMS, max_len=_MAX_TERM)
    if terms:
        voice["terminology"] = "use the firm's terms where the facts support them: " + ", ".join(terms)
    rules = _clean_list(voice_profile.get("rules"), max_items=_MAX_RULES, max_len=_MAX_VALUE)
    if rules:
        voice["rules"] = "; ".join(rules)
    return voice


def voice_from_platform(payload: object) -> dict[str, str]:
    """The agent voice lines from the current `platform_defaults` voice payload."""
    if not isinstance(payload, dict):
        return {}
    voice: dict[str, str] = {}
    for key, value in payload.items():
        if key in _PLATFORM_META_KEYS:
            continue
        text = _clean(value)
        if text:
            voice[str(key)] = text
    return voice


def firm_name(brand_profile: object, company: object) -> str:
    brand = brand_profile.get("brand") if isinstance(brand_profile, dict) else None
    name = _clean(brand.get("name"), 80) if isinstance(brand, dict) else None
    if name and name.lower() not in _PLACEHOLDER_NAMES:
        return name
    company_name = _clean(company, 80)
    return company_name or NEUTRAL_FIRM


def build_house_style(
    *,
    voice_profile: object,
    platform_voice: object,
    brand_profile: object,
    company: object,
) -> dict[str, Any]:
    """The `HouseStyleProfile` dict for one tenant. `voice_source` records which path won."""
    voice = voice_from_profile(voice_profile)
    source = "tenant_voice_profile"
    if not voice:
        voice = voice_from_platform(platform_voice)
        source = "platform_default" if voice else "none"
    profile = HouseStyleProfile(
        client=firm_name(brand_profile, company),
        paper_title=PAPER_TITLE,
        sections=[],
        voice=voice,
        boilerplate={},
        section_exemplars={},
        section_playbooks={},
        source_papers=[],
    )
    return {**profile.model_dump(), "voice_source": source}
