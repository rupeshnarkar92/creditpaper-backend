"""A deal's template: options, resolution, snapshot, change and lock (spec 2026-09-15 §1–2).

Resolution order for a new deal:
  1. the firm template whose paper types include the deal type (`firm_type_match`)
  2. the firm default template (`firm_default`)
  3. the Creditpaper Standard template from `platform_defaults` (`platform_standard`)

Only active templates of the caller's tenant are ever considered; templates with no
sections are not auto-suggested (an analyst may still pick one). The deal stores a
snapshot so a later Library edit never reshapes a deal in progress. A deal with no
snapshot (created before this change) resolves at read time — firm default, else
Standard — and is written only when the template is changed or the deal is generated.
The template is locked once any `deal_paper_sections` row exists.
"""

from __future__ import annotations

import re
from typing import Any

from creditpaper.deals.store import deal_template_store
from creditpaper.paper.defaults_store import load_current
from creditpaper.templates.store import template_store

STANDARD_ID = "platform:standard"
STANDARD_SOURCE = "platform"

REASON_TYPE_MATCH = "firm_type_match"
REASON_FIRM_DEFAULT = "firm_default"
REASON_STANDARD = "platform_standard"
SOURCE_ANALYST = "analyst_choice"

TEMPLATE_UNAVAILABLE_MESSAGE = "This template is no longer available. Choose another."


class TemplateUnavailable(Exception):
    """Unknown, inactive or another tenant's template (422 TEMPLATE_UNAVAILABLE)."""


class TemplateLocked(Exception):
    """The deal has been generated; its template is fixed (409 TEMPLATE_LOCKED)."""


class DealNotFound(Exception):
    """The deal does not exist in the caller's tenant (404)."""


# --- pure ---------------------------------------------------------------------


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "section"


def normalise_sections(sections: object) -> list[dict[str, Any]]:
    """Template sections as `[{number, name, subsections:[{number, name}], engine_section?}]`.

    Tolerates the older `[{number, name}]` rows and drops anything malformed.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(sections, list):
        return out
    for i, s in enumerate(sections, start=1):
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or s.get("title") or "").strip()
        if not name:
            continue
        subs = []
        for j, sub in enumerate(s.get("subsections") or [], start=1):
            if isinstance(sub, dict) and str(sub.get("name") or "").strip():
                subs.append(
                    {"number": str(sub.get("number") or f"{i}.{j}"), "name": str(sub["name"]).strip()}
                )
        item: dict[str, Any] = {
            "number": str(s.get("number") or f"{i:02d}"),
            "name": name,
            "subsections": subs,
        }
        engine = s.get("engine_section")
        if isinstance(engine, int) and not isinstance(engine, bool):
            item["engine_section"] = engine
        out.append(item)
    return out


def build_snapshot(name: str, sections: object, version_ref: dict[str, Any]) -> dict[str, Any]:
    """`{name, version_ref, sections:[{key, number, name, subsections, engine_section?}]}`.

    `key` is the slug of the name plus the 1-based position: stable for the life of
    the snapshot, unique inside it.
    """
    snap_sections = []
    for position, s in enumerate(normalise_sections(sections), start=1):
        snap_sections.append({"key": f"{_slug(s['name'])}-{position}", **s})
    return {"name": name, "version_ref": version_ref, "sections": snap_sections}


def _firm_version_ref(row: dict[str, Any]) -> dict[str, Any]:
    updated = row.get("updated_at")
    return {
        "kind": "firm",
        "template_id": str(row["id"]),
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
    }


def firm_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return build_snapshot(str(row.get("name") or "Template"), row.get("sections"), _firm_version_ref(row))


def standard_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload") or {}
    return build_snapshot(
        str(payload.get("name") or "Creditpaper Standard"),
        payload.get("sections"),
        {"kind": "platform", "platform_defaults_version": int(record.get("version") or 0)},
    )


def _usable(row: dict[str, Any]) -> bool:
    return bool(row.get("is_active")) and bool(normalise_sections(row.get("sections")))


def choose_template(rows: list[dict[str, Any]], deal_type: str | None) -> tuple[dict[str, Any] | None, str]:
    """Pure resolution over the tenant's template rows. Returns (row | None, reason)."""
    active = [r for r in rows if _usable(r)]
    if deal_type:
        wanted = deal_type.strip().lower()
        # Custom templates before the default, each group in the store's order.
        for row in sorted(active, key=lambda r: bool(r.get("is_default"))):
            types = {str(t).strip().lower() for t in (row.get("paper_types") or [])}
            if wanted in types:
                return row, REASON_TYPE_MATCH
    for row in active:
        if row.get("is_default"):
            return row, REASON_FIRM_DEFAULT
    return None, REASON_STANDARD


def choose_read_time(rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """For a deal with no snapshot: firm default, else Standard."""
    return choose_template(rows, None)


# --- DB-backed ----------------------------------------------------------------


def _tenant_rows(tenant_id: str) -> list[dict[str, Any]]:
    listed = template_store.list_for_tenant(tenant_id)
    rows = list(listed.get("custom") or [])
    if listed.get("default"):
        rows.append(listed["default"])
    return rows


def _option(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row.get("name") or "Template",
        "section_count": len(normalise_sections(row.get("sections"))),
        "paper_types": list(row.get("paper_types") or []),
        "is_default": bool(row.get("is_default")),
        "source": row.get("source") or "custom",
    }


def _standard_option(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload") or {}
    return {
        "id": STANDARD_ID,
        "name": str(payload.get("name") or "Creditpaper Standard"),
        "section_count": len(normalise_sections(payload.get("sections"))),
        "paper_types": [],
        "is_default": False,
        "source": STANDARD_SOURCE,
    }


def list_template_options(tenant_id: str, deal_type: str | None) -> dict[str, Any]:
    rows = _tenant_rows(tenant_id)
    standard = load_current("template")
    chosen, reason = choose_template(rows, deal_type)
    active = [r for r in rows if r.get("is_active")]
    # Default first, then the rest in the store's order; Standard last.
    active.sort(key=lambda r: not bool(r.get("is_default")))
    return {
        "templates": [_option(r) for r in active] + [_standard_option(standard)],
        "suggested_id": str(chosen["id"]) if chosen else STANDARD_ID,
        "suggested_reason": reason,
    }


def _lookup(tenant_id: str, template_id: str) -> dict[str, Any]:
    """A firm template row the tenant may use, or raise TemplateUnavailable."""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", template_id or ""):
        raise TemplateUnavailable(template_id)
    row = template_store.get_by_id(tenant_id, template_id)
    if not row or not row.get("is_active") or str(row.get("tenant_id")).lower() != str(tenant_id).lower():
        raise TemplateUnavailable(template_id)
    return row


def get_outline(tenant_id: str, template_id: str) -> dict[str, Any]:
    if template_id == STANDARD_ID:
        record = load_current("template")
        snap = standard_snapshot(record)
        source = STANDARD_SOURCE
    else:
        row = _lookup(tenant_id, template_id)
        snap = firm_snapshot(row)
        source = row.get("source") or "custom"
    return {
        "id": template_id,
        "name": snap["name"],
        "source": source,
        "section_count": len(snap["sections"]),
        "sections": [_public_section(s) for s in snap["sections"]],
    }


def _public_section(s: dict[str, Any]) -> dict[str, Any]:
    """A snapshot section without the hidden engine mapping."""
    return {"key": s["key"], "number": s["number"], "name": s["name"], "subsections": s["subsections"]}


def resolve_for_new_deal(
    tenant_id: str, deal_type: str, template_id: str | None
) -> tuple[str | None, str, dict[str, Any]]:
    """(deals.template_id, deals.template_source, deals.template_snapshot) for a new deal.

    An explicit `template_id` is the analyst's pick from the New Deal form; the source
    records what the server would have suggested when they kept the suggestion, and
    `analyst_choice` when they picked something else.
    """
    rows = _tenant_rows(tenant_id)
    suggested, reason = choose_template(rows, deal_type)
    suggested_id = str(suggested["id"]) if suggested else STANDARD_ID
    if template_id is None or template_id == suggested_id:
        if suggested is None:
            return None, REASON_STANDARD, standard_snapshot(load_current("template"))
        return str(suggested["id"]), reason, firm_snapshot(suggested)
    if template_id == STANDARD_ID:
        return None, SOURCE_ANALYST, standard_snapshot(load_current("template"))
    row = _lookup(tenant_id, template_id)
    return str(row["id"]), SOURCE_ANALYST, firm_snapshot(row)


def effective_template(tenant_id: str, deal: dict[str, Any]) -> tuple[str | None, str, dict[str, Any]]:
    """The template a deal is shown with: its snapshot, or the read-time resolution."""
    snap = deal.get("template_snapshot")
    if isinstance(snap, dict) and isinstance(snap.get("sections"), list):
        return deal.get("template_id"), str(deal.get("template_source") or SOURCE_ANALYST), snap
    chosen, reason = choose_read_time(_tenant_rows(tenant_id))
    if chosen is None:
        return None, reason, standard_snapshot(load_current("template"))
    return str(chosen["id"]), reason, firm_snapshot(chosen)


def template_ref(template_id: str | None, snapshot: dict[str, Any]) -> dict[str, Any]:
    """What the audit trail records about a deal's template."""
    return {
        "template_id": template_id or STANDARD_ID,
        "name": snapshot.get("name"),
        "version_ref": snapshot.get("version_ref"),
    }


def change_template(tenant_id: str, deal_id: str, template_id: str, actor_id: str | None) -> dict[str, Any]:
    deal = deal_template_store.get_deal(tenant_id, deal_id)
    if deal is None:
        raise DealNotFound(deal_id)
    if deal.get("has_paper_sections"):
        raise TemplateLocked(deal_id)
    before_id, before_source, before_snap = effective_template(tenant_id, deal)
    if template_id == STANDARD_ID:
        new_id, snap = None, standard_snapshot(load_current("template"))
    else:
        row = _lookup(tenant_id, template_id)
        new_id, snap = str(row["id"]), firm_snapshot(row)
    outcome = deal_template_store.change_template(
        tenant_id=tenant_id,
        deal_id=deal_id,
        template_id=new_id,
        template_source=SOURCE_ANALYST,
        snapshot=snap,
        actor_id=actor_id,
        audit_metadata={
            "from": {**template_ref(before_id, before_snap), "source": before_source},
            "to": {**template_ref(new_id, snap), "source": SOURCE_ANALYST},
        },
    )
    if outcome == "not_found":
        raise DealNotFound(deal_id)
    if outcome == "locked":
        raise TemplateLocked(deal_id)
    return {
        "deal_id": deal_id,
        "template_id": new_id or STANDARD_ID,
        "template": {
            "id": new_id or STANDARD_ID,
            "name": snap["name"],
            "source": SOURCE_ANALYST,
            "locked": False,
        },
        "sections": [_public_section(s) for s in snap["sections"]],
    }


def ensure_snapshot(tenant_id: str, deal_id: str) -> dict[str, Any]:
    """Write the read-time template as the deal's snapshot if it has none (first
    generation). Returns the deal's effective snapshot. Raises DealNotFound."""
    deal = deal_template_store.get_deal(tenant_id, deal_id)
    if deal is None:
        raise DealNotFound(deal_id)
    template_id, source, snap = effective_template(tenant_id, deal)
    if not isinstance(deal.get("template_snapshot"), dict):
        deal_template_store.write_snapshot_if_absent(
            tenant_id=tenant_id,
            deal_id=deal_id,
            template_id=template_id,
            template_source=source,
            snapshot=snap,
        )
    return snap
