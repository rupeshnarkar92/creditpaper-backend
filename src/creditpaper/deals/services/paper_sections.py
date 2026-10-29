"""The paper-sections API: the deal's template joined with engine output (spec 2026-09-15 §3).

Order, titles and subsections come from the deal's template snapshot. Generation keeps
writing engine sections by number into `deal_paper_sections`; the section resolver
decides which template section shows which engine section. Template sections no
engine section can write come back `not_written`.
"""

from __future__ import annotations

from typing import Any

from creditpaper.deals.services import deal_templates
from creditpaper.deals.services.section_resolver import map_sections
from creditpaper.deals.store import deal_template_store

NOT_WRITTEN = "not_written"
PENDING = "pending"


class SectionNotFound(Exception):
    """No section with this key in the deal's template."""


class SectionNotWritable(Exception):
    """The section exists but no engine section can write it yet."""


def join_sections(snapshot: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure: snapshot sections + `deal_paper_sections` rows -> response sections."""
    sections = snapshot.get("sections") or []
    mapping = map_sections(sections)
    by_number = {int(r["section"]): r for r in rows}
    out = []
    for s in sections:
        engine = mapping.get(str(s["key"]))
        row = by_number.get(engine) if engine is not None else None
        if engine is None:
            status, prose, citations, error = NOT_WRITTEN, None, [], None
        elif row is None:
            status, prose, citations, error = PENDING, None, [], None
        else:
            status, prose, citations, error = (
                row["status"],
                row.get("prose"),
                row.get("citations") or [],
                row.get("error"),
            )
        out.append(
            {
                "key": s["key"],
                "number": s["number"],
                "title": s["name"],
                "subsections": s.get("subsections") or [],
                "status": status,
                "prose": prose,
                "citations": citations,
                "error": error,
                "editor_value": row.get("editor_value") if row else None,
                "edit_revision": row.get("edit_revision", 0) if row else 0,
            }
        )
    return out


def get_paper_sections(tenant_id: str, deal_id: str) -> dict[str, Any]:
    deal = deal_template_store.get_deal(tenant_id, deal_id)
    if deal is None:
        raise deal_templates.DealNotFound(deal_id)
    template_id, source, snapshot = deal_templates.effective_template(tenant_id, deal)
    rows = deal_template_store.list_paper_section_rows(tenant_id, deal_id)
    return {
        "deal_id": deal_id,
        "active_job": deal_template_store.active_generation(tenant_id, deal_id),
        "template": {
            "id": template_id or deal_templates.STANDARD_ID,
            "name": snapshot.get("name"),
            "source": source,
            "locked": bool(rows) or bool(deal.get("has_paper_sections")),
        },
        "sections": join_sections(snapshot, rows),
    }


def paper_sections_from_raw(deal_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Pure paper-sections response builder for get_workspace_page()."""
    template = raw.get("template") or {}
    snapshot = template.get("snapshot") or {}
    rows = raw.get("rows") or []
    return {
        "deal_id": deal_id,
        "active_job": template.get("active_job"),
        "template": {
            "id": template.get("id") or deal_templates.STANDARD_ID,
            "name": snapshot.get("name"),
            "source": template.get("source") or deal_templates.STANDARD_SOURCE,
            "locked": bool(rows) or bool(template.get("active_job")),
        },
        "sections": join_sections(snapshot, rows),
    }


def keyed_job_sections(snapshot: dict[str, Any], engine_sections: dict[str, Any]) -> dict[str, Any]:
    """Pure: a generation job's per-section values, keyed by engine number, re-keyed by
    template section key. Engine sections no template section shows are dropped."""
    mapping = map_sections(snapshot.get("sections") or [])
    return {
        key: engine_sections[str(engine)]
        for key, engine in mapping.items()
        if engine is not None and str(engine) in engine_sections
    }


def job_status_by_key(tenant_id: str, deal_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """The generation status response with `sections` and `timing.sections` keyed by
    template section key, matching GET /paper/sections. Raises DealNotFound."""
    deal = deal_template_store.get_deal(tenant_id, deal_id)
    if deal is None:
        raise deal_templates.DealNotFound(deal_id)
    _template_id, _source, snapshot = deal_templates.effective_template(tenant_id, deal)
    out = dict(job)
    out["sections"] = keyed_job_sections(snapshot, job.get("sections") or {})
    timing = job.get("timing")
    if isinstance(timing, dict) and isinstance(timing.get("sections"), dict):
        out["timing"] = {**timing, "sections": keyed_job_sections(snapshot, timing["sections"])}
    return out


def section_ref_for_engine(snapshot: dict[str, Any], engine_number: int) -> dict[str, Any]:
    """Pure: the template section (key, title) an engine section shows as, for Activity.
    The first template section mapped to it wins; none -> key None, a generic title."""
    sections = snapshot.get("sections") or []
    mapping = map_sections(sections)
    for s in sections:
        if mapping.get(str(s["key"])) == engine_number:
            return {"key": s["key"], "title": s.get("name")}
    return {"key": None, "title": f"Section {engine_number}"}


def section_ref(tenant_id: str, deal_id: str, engine_number: int) -> dict[str, Any]:
    """The deal's template section for an engine section number. Raises DealNotFound."""
    deal = deal_template_store.get_deal(tenant_id, deal_id)
    if deal is None:
        raise deal_templates.DealNotFound(deal_id)
    _template_id, _source, snapshot = deal_templates.effective_template(tenant_id, deal)
    return section_ref_for_engine(snapshot, engine_number)


def engine_section_for(tenant_id: str, deal_id: str, section: str) -> tuple[int, str | None]:
    """Translate a regenerate target to an engine section number.

    `section` is a template section key, or (backwards compatible) an engine section
    number as digits. Returns (engine number, key or None). Writes the snapshot if
    the deal has none, since regeneration is a generation.
    """
    deal = deal_template_store.get_deal(tenant_id, deal_id)
    if deal is None:
        raise deal_templates.DealNotFound(deal_id)
    _template_id, _source, snapshot = deal_templates.effective_template(tenant_id, deal)
    if section.isdigit():
        engine: int | None = int(section)
        key = None
    else:
        sections = snapshot.get("sections") or []
        if not any(str(s.get("key")) == section for s in sections):
            raise SectionNotFound(section)
        engine = map_sections(sections).get(section)
        if engine is None:
            raise SectionNotWritable(section)
        key = section
    # Only a request that will regenerate fixes the template.
    deal_templates.ensure_snapshot(tenant_id, deal_id)
    return int(engine), key
