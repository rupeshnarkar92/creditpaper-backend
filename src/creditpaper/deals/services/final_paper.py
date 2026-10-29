"""Read and render immutable approval manifests, never the current paper."""

from __future__ import annotations

from datetime import UTC, date, datetime
from io import BytesIO
from typing import Any

from creditpaper.db import tenant_cursor, tenant_uuid
from creditpaper.deals.services.deal_templates import standard_snapshot
from creditpaper.deals.services.paper_sections import join_sections
from creditpaper.paper.theme import resolve_theme


class FinalPaperError(Exception):
    def __init__(self, code: str, message: str, status: int = 404):
        super().__init__(message)
        self.code, self.status = code, status


def freeze_paper(
    cur,
    tenant_id,
    deal_id,
    rows,
    profile,
    *,
    outstanding_items=None,
    open_contradictions=None,
    template_override=None,
):
    """Capture display content in the approval transaction, using its cursor only."""
    theme_default = None
    if template_override is None:
        cur.execute(
            "SELECT name,template_id,template_source,template_snapshot FROM deals "
            "WHERE tenant_id=%s AND id=%s",
            (tenant_id, deal_id),
        )
        name, template_id, source, snapshot = cur.fetchone()
    else:
        template_id = template_override["id"]
        source = template_override["source"]
        snapshot = template_override["snapshot"]
        name = template_override.get("name") or "Approved paper"
        theme_default = template_override.get("theme_default")
        if isinstance(theme_default, dict):
            theme_default = (theme_default.get("version"), theme_default.get("payload"))
    if not snapshot:
        cur.execute("SELECT version,payload FROM platform_defaults WHERE kind='template' AND is_current=true")
        default = cur.fetchone()
        snapshot = standard_snapshot({"version": default[0], "payload": default[1]}) if default else None
    records = [
        {
            "section": row[0],
            "status": row[1],
            "prose": row[2],
            "citations": row[3],
            "editor_value": row[5],
            "edit_revision": row[6],
        }
        for row in rows
    ]
    sections = (
        join_sections(snapshot, records) if snapshot and snapshot.get("sections") else _legacy_sections(rows)
    )
    if theme_default is None:
        cur.execute(
            "SELECT version,payload FROM platform_defaults WHERE kind='paper_theme' AND is_current=true"
        )
        theme_default = cur.fetchone()
    default = theme_default
    theme = (
        resolve_theme(
            profile.get("brand_profile"),
            {"version": default[0], "payload": default[1]},
            logo_endpoint=None,
            firm_name_fallback=profile.get("firm_name"),
        )
        if default
        else None
    )
    return {
        "name": name,
        "template": {
            "id": str(template_id) if template_id else "platform:standard",
            "name": (snapshot or {}).get("name", "Approved paper"),
            "source": source or "platform",
            "version_ref": (snapshot or {}).get("version_ref"),
            "locked": True,
        },
        "sections": sections,
        "theme": theme,
        "outstanding_items": outstanding_items or [],
        "open_contradictions": open_contradictions or [],
    }


def _legacy_sections(rows):
    from creditpaper.renderer.docx_render import _section_titles

    titles = _section_titles(None)
    return [
        {
            "key": f"section-{row[0]}",
            "number": str(row[0]),
            "title": titles.get(row[0], f"Section {row[0]}"),
            "subsections": [],
            "status": row[1],
            "prose": row[2],
            "citations": row[3] or [],
            "error": None,
            "editor_value": None,
            "edit_revision": 0,
        }
        for row in rows
    ]


def get_draft_paper(tenant_id: str, deal_id: str) -> dict:
    """Build a current, non-approved export from live deal sections and the locked template."""
    from creditpaper.deals.services.section_resolver import map_sections

    tid = str(tenant_uuid(tenant_id))
    with tenant_cursor(tid) as cur:
        cur.execute("SELECT current_state FROM deals WHERE tenant_id=%s AND id=%s", (tid, deal_id))
        deal = cur.fetchone()
        if not deal:
            raise FinalPaperError("DEAL_NOT_FOUND", "Deal not found")
        cur.execute("SELECT public.get_approval_paper_data(%s::uuid, %s::text, NULL::uuid)", (tid, deal_id))
        row = cur.fetchone()
    data = row[0] if row and row[0] else None
    if isinstance(data, str):
        import json

        data = json.loads(data)
    if not isinstance(data, dict):
        raise FinalPaperError("DRAFT_PAPER_NOT_READY", "The draft paper could not be loaded")
    data["actor_id"] = None
    from creditpaper.ledger.service import _approval_template_identity

    snapshot, template_id, source = _approval_template_identity(data)
    template_sections = snapshot.get("sections") or []
    mapped = map_sections(template_sections)
    selected = sorted({number for number in mapped.values() if number is not None})
    rows = {int(item[0]): tuple(item) for item in data.get("sections", [])}
    section_rows = [rows.get(number, (number, "pending", None, [], None, None, 0)) for number in selected]
    paper = freeze_paper(
        None,
        tid,
        deal_id,
        section_rows,
        data.get("profile") or {},
        template_override={
            "id": template_id,
            "source": source,
            "snapshot": snapshot,
            "name": data.get("name") or "Draft paper",
            "theme_default": data.get("theme_default"),
        },
    )
    recommendation = data.get("recommendation")
    if recommendation:
        recommendation = {
            **recommendation,
            "id": str(recommendation["id"]),
            "author_id": str(recommendation["author_id"]),
            "author_name": str(recommendation.get("author_name") or "Former member"),
        }
    return {
        **paper,
        "deal_id": deal_id,
        "recommendation": recommendation,
        "draft": True,
        "current_state": deal[0],
    }


def get_final_paper(tenant_id: str, deal_id: str) -> dict:
    tid = str(tenant_uuid(tenant_id))
    with tenant_cursor(tid) as cur:
        cur.execute("SELECT current_state FROM deals WHERE tenant_id=%s AND id=%s", (tid, deal_id))
        deal = cur.fetchone()
        if not deal:
            raise FinalPaperError("DEAL_NOT_FOUND", "Deal not found")
        if deal[0] not in {"APPROVED", "ISSUED", "ARCHIVED"}:
            raise FinalPaperError("FINAL_PAPER_NOT_READY", "The current paper has not been approved")
        cur.execute(
            "SELECT id,actor_id,valid_at,known_at,ledger_revision,digest,manifest FROM ledger_approvals "
            "WHERE tenant_id=%s AND deal_id=%s ORDER BY known_at DESC,id DESC LIMIT 1",
            (tid, deal_id),
        )
        row = cur.fetchone()
    if not row:
        raise FinalPaperError("FINAL_PAPER_NOT_READY", "No approved paper exists yet")
    approval_id, actor_id, valid_at, known_at, revision, digest, manifest = row
    manifest_approval = manifest.get("approval") or {}
    paper = manifest.get("paper") or {
        "name": "Approved Credit Paper",
        "template": None,
        "sections": _legacy_sections(manifest.get("sections", [])),
        "theme": None,
    }
    result = {
        **paper,
        "outstanding_items": manifest.get("outstanding_items", paper.get("outstanding_items", [])),
        "open_contradictions": manifest.get("open_contradictions", paper.get("open_contradictions", [])),
        "deal_id": deal_id,
        "read_only": True,
        "recommendation": manifest.get("recommendation"),
        "approval": {
            "id": str(approval_id),
            "actor_id": str(actor_id),
            "actor_name": str(manifest_approval.get("actor_name") or "Former member"),
            "approved_at": known_at,
            "valid_at": valid_at,
            "ledger_revision": revision,
            "digest": digest,
        },
        "legacy_snapshot": "paper" not in manifest,
        "_export_brand_profile": (manifest.get("profile") or {}).get("brand_profile"),
    }
    return result


def render_final_docx(paper: dict, *, logo_bytes: bytes | None = None, draft: bool = False) -> bytes:
    """Deterministic DOCX export; drafts are conspicuously watermarked and never signed."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
    from docx.shared import Pt

    from creditpaper.renderer.docx_render import (
        _header,
        _render_approval,
        _render_cover,
        _render_md_table,
    )
    from creditpaper.renderer.paper_spec import PaperSpec, apply_paper_spec

    theme = paper.get("theme") or {}
    spec = PaperSpec.from_theme(theme, logo_bytes=logo_bytes if logo_bytes else None)
    doc = Document()
    apply_paper_spec(doc, spec)
    model = _export_model(paper)
    title = str(paper.get("paper_title") or "Internal Credit Memorandum")
    approval = paper.get("approval") or {}
    approval_date = _as_date(approval.get("approved_at") or approval.get("valid_at"))
    template = paper.get("template") or {}
    version_ref = template.get("version_ref") if isinstance(template, dict) else None
    if isinstance(version_ref, dict):
        if version_ref.get("template_revision") is not None:
            version_text = f"{template.get('name', 'Template')} · revision {version_ref['template_revision']}"
        elif version_ref.get("updated_at"):
            version_text = f"{template.get('name', 'Template')} · {version_ref['updated_at']}"
        elif version_ref.get("platform_defaults_version") is not None:
            version_text = (
                f"{template.get('name', 'Template')} · platform revision "
                f"{version_ref['platform_defaults_version']}"
            )
        else:
            version_text = str(template.get("name") or template.get("id") or "Unversioned template")
    else:
        version_text = str(template.get("name") or template.get("id") or "Unversioned template")
    export_status = "DRAFT — NOT APPROVED" if draft else "APPROVED"
    if draft:
        approval_date = date.today()
    _header(doc, model, title, spec, status=export_status, version_reference=version_text)
    _render_cover(
        doc,
        model,
        title,
        spec,
        approval_date,
        status="DRAFT" if draft else "APPROVED",
        version_reference=version_text,
        approval_id="" if draft else str(approval.get("id") or ""),
        include_firm_name_with_logo=logo_bytes is not None,
    )
    if draft:
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import RGBColor

        for section in doc.sections:
            paragraph = section.header.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = paragraph.add_run("DRAFT — NOT APPROVED")
            run.bold = True
            run.font.size = Pt(18)
            run.font.color.rgb = RGBColor(*spec.draft_rgb)

    def leaves(paragraph, node):
        if "text" not in node:
            for child in node.get("children", []):
                leaves(paragraph, child)
            return
        run = paragraph.add_run(node["text"])
        run.bold, run.italic = bool(node.get("bold")), bool(node.get("italic"))
        run.underline = bool(node.get("underline"))
        run.font.strike = bool(node.get("strikethrough"))
        if node.get("code"):
            run.font.name = "Courier New"
        if node.get("highlight"):
            run.font.highlight_color = WD_COLOR_INDEX.YELLOW
        size = str(node.get("fontSize", "")).removesuffix("px")
        try:
            run.font.size = Pt(max(6, min(54, float(size) * 0.75)))
        except ValueError:
            pass

    def block(container, node):
        if node.get("type") == "table":
            rows = node.get("children", [])
            cols = max((len(r.get("children", [])) for r in rows), default=0)
            if not rows or not cols:
                return
            table = container.add_table(rows=len(rows), cols=cols)
            for i, row in enumerate(rows):
                for j, cell in enumerate(row.get("children", [])):
                    target = table.cell(i, j)
                    children = cell.get("children", [])
                    if any("text" in child for child in children):
                        leaves(target.paragraphs[0], cell)
                    else:
                        for child in children:
                            block(target, child)
            return
        paragraph = container.add_paragraph()
        kind = node.get("type")
        if kind in {"heading", "subheading"}:
            paragraph.style = "Heading 2" if kind == "heading" else "Heading 3"
        paragraph.alignment = {
            "left": WD_ALIGN_PARAGRAPH.LEFT,
            "center": WD_ALIGN_PARAGRAPH.CENTER,
            "right": WD_ALIGN_PARAGRAPH.RIGHT,
        }.get(node.get("align"))
        leaves(paragraph, node)

    for section in paper["sections"]:
        doc.add_heading(f"{section['number']}. {section['title']}", 1)
        if section.get("editor_value"):
            for node in section["editor_value"]:
                block(doc, node)
        else:
            lines = (section.get("prose") or "Not written yet").splitlines()
            i = 0
            while i < len(lines):
                text = lines[i].strip()
                if text.startswith("|"):
                    table = []
                    while i < len(lines) and lines[i].strip().startswith("|"):
                        table.append(lines[i])
                        i += 1
                    _render_md_table(doc, table, spec)
                    continue
                if text.startswith("### "):
                    doc.add_heading(text[4:], 3)
                elif text.startswith("## "):
                    doc.add_heading(text[3:], 2)
                elif text:
                    doc.add_paragraph(text)
                i += 1
    open_contradictions = paper.get("open_contradictions") or []
    if open_contradictions:
        doc.add_page_break()
        doc.add_heading("Annexure — Open Contradictions", 1)
        doc.add_paragraph("These non-material register entries were reviewed and acknowledged at approval.")
        for item in open_contradictions:
            doc.add_paragraph(str(item.get("concept_path") or "Contradiction"), style="List Bullet")
            for source in item.get("assertions", []):
                doc.add_paragraph(
                    f"{source.get('document_name', 'Source')}: {source.get('value')}", style="List Bullet 2"
                )

    outstanding_items = paper.get("outstanding_items") or []
    if outstanding_items:
        doc.add_page_break()
        doc.add_heading("Annexure — Outstanding Items", 1)
        doc.add_paragraph(
            "The approver acknowledged these document gaps. They were excluded from approval blockers."
        )
        for item in outstanding_items:
            doc.add_paragraph(
                f"{item.get('document_name', 'Document')}: {item.get('description', 'Outstanding item.')}",
                style="List Bullet",
            )

    recommendation = paper.get("recommendation")
    if recommendation:
        doc.add_heading("Signed Recommendation", 2)
        doc.add_paragraph(str(recommendation["body"]))
        author_name = str(recommendation.get("author_name") or "Former member")
        doc.add_paragraph(f"Signed by {author_name} · {_format_timestamp(recommendation.get('signed_at'))}")
    if not draft:
        _render_approval(doc, spec, committee=None, approval=approval)
    stream = BytesIO()
    doc.save(stream)
    return stream.getvalue()


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return date.today()


def _format_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return "unknown date"


def _export_model(paper: dict[str, Any]) -> Any:
    """Small DealModel identity adapter so DOCX cover/header reuse the canonical renderer."""
    from creditpaper.model.core import Fact
    from creditpaper.model.deal import DealModel

    name = str(paper.get("name") or "Approved Credit Paper")
    deal_id = str(paper.get("deal_id") or name)
    return DealModel(
        tenant_id="export",
        deal_id=deal_id,
        deal={"name": Fact(value=name)},
    )


def final_paper_logo_bytes(paper: dict[str, Any]) -> bytes | None:
    """Fetch only the brand logo URL frozen into the approval manifest; return DOCX-compatible bytes."""
    from creditpaper.paper.logo_fetch import fetch_logo
    from creditpaper.paper.theme import brand_logo_url
    from creditpaper.renderer.paper_spec import DOCX_IMAGE_TYPES

    url = brand_logo_url(paper.get("_export_brand_profile"))
    if not url:
        return None
    logo = fetch_logo(url)
    if logo is None or logo.content_type not in DOCX_IMAGE_TYPES:
        return None
    return logo.content


def final_paper_actor_names(tenant_id: str, actor_ids: list[str]) -> dict[str, str]:
    """Resolve a set of paper signers in one stored-procedure call."""
    from uuid import UUID

    from creditpaper.db import tenant_cursor, tenant_uuid

    normalised = sorted({str(UUID(actor_id)) for actor_id in actor_ids})
    if not normalised:
        return {}
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(tid) as cur:
        cur.execute(
            "SELECT public.get_final_paper_signer_names(%s::uuid, %s::uuid[])",
            (str(tid), normalised),
        )
        row = cur.fetchone()
    names = row[0] if row and isinstance(row[0], dict) else {}
    return {actor_id: str(names.get(actor_id) or "Former member") for actor_id in normalised}


def public_final_paper(paper: dict[str, Any]) -> dict[str, Any]:
    """Remove server-only export metadata before returning the JSON endpoint."""
    return {key: value for key, value in paper.items() if not key.startswith("_")}
