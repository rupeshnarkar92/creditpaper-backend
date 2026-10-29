"""One-call workspace-page response assembly."""

from __future__ import annotations

from typing import Any

from creditpaper.deals.services.activity import activity_from_raw
from creditpaper.deals.services.fact_sheet import fact_sheet_from_raw
from creditpaper.deals.services.paper_sections import paper_sections_from_raw
from creditpaper.deals.services.slot_board import board_from_raw
from creditpaper.deals.services.workspace import DealNotFound, workspace_from_raw
from creditpaper.paper.theme import resolve_theme


def _theme(deal_id: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    if not raw:
        return None
    default = raw.get("default_record")
    if not isinstance(default, dict):
        return None
    return resolve_theme(
        raw.get("brand_profile"),
        default,
        logo_endpoint=f"/api/deals/{deal_id}/paper/logo",
        firm_name_fallback=raw.get("firm_name"),
    )


def build_workspace_page(tenant_id: str, deal_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Convert one SQL payload into existing endpoint contracts; performs no DB reads."""
    workspace_raw = raw.get("workspace") or {}
    if workspace_raw.get("deal") is None:
        raise DealNotFound(deal_id)
    return {
        "workspace": workspace_from_raw(workspace_raw),
        "board": board_from_raw(tenant_id, deal_id, raw.get("board") or {}),
        "fact_sheet": fact_sheet_from_raw(deal_id, raw.get("fact_sheet") or {}),
        "activity": activity_from_raw(deal_id, raw.get("activity") or {}),
        "reviews": raw.get("reviews") or [],
        "sections": paper_sections_from_raw(deal_id, raw.get("paper") or {}),
        "theme": _theme(deal_id, raw.get("theme") or {}),
        "recommendation": raw.get("recommendation") or {"current": None, "history_count": 0},
    }
