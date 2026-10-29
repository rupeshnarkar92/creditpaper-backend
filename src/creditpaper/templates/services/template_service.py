"""Template service — orchestrates the templates domain.

Reuses:
  - onboarding.services.document_service for paper classification
  - onboarding.routes.preview_doc for the .docx rendering (extract coming later)
"""

from __future__ import annotations

import logging
from typing import Any

from creditpaper.templates.store import template_store

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paper types
# ---------------------------------------------------------------------------


def list_paper_types() -> list[dict[str, Any]]:
    return template_store.list_paper_types(only_active=True)


# ---------------------------------------------------------------------------
# Template CRUD
# ---------------------------------------------------------------------------


def list_templates(tenant_id: str) -> dict[str, Any]:
    """Return default + custom + paper_types (all in one response)."""
    rows = template_store.list_for_tenant(tenant_id)
    return {
        "default": rows["default"],
        "custom": rows["custom"],
        "paper_types": template_store.list_paper_types(only_active=True),
    }


def get_template(tenant_id: str, template_id: str) -> dict[str, Any] | None:
    return template_store.get_by_id(tenant_id, template_id)


def _normalize_type_set(paper_types: list[str] | None) -> list[str]:
    """Normalize a paper-type selection for duplicate comparison."""
    return sorted({t.strip().lower() for t in (paper_types or []) if t and str(t).strip()})


def _find_duplicate_type_template(
    tenant_id: str, paper_types: list[str] | None, exclude_id: str | None = None
) -> dict[str, Any] | None:
    """Return an existing custom template with the identical paper-type set, if any."""
    wanted = _normalize_type_set(paper_types)
    if not wanted:
        return None
    rows = template_store.list_for_tenant(tenant_id)
    for row in rows.get("custom", []):
        if exclude_id and str(row.get("id")) == str(exclude_id):
            continue
        if _normalize_type_set(row.get("paper_types")) == wanted:
            return row
    return None


def create_template(
    tenant_id: str,
    *,
    name: str,
    description: str | None = None,
    paper_types: list[str] | None = None,
) -> dict[str, Any]:
    """Create a custom template.

    Raises:
        ValueError("DUPLICATE_PAPER_TYPE_TEMPLATE"): tenant already has a
            custom template with the identical paper-type selection.
    """
    if _find_duplicate_type_template(tenant_id, paper_types) is not None:
        raise ValueError("DUPLICATE_PAPER_TYPE_TEMPLATE")
    return template_store.create(
        tenant_id,
        name=name,
        description=description,
        paper_types=paper_types or [],
        is_default=False,
        source="custom",
    )


def _is_editable(template: dict[str, Any] | None) -> bool:
    """Default and predefined are editable but not deletable; custom is fully editable."""
    if not template:
        return False
    # Editable means can rename/sections/paper_types — default/predefined now allowed
    return True


def _is_deletable(template: dict[str, Any] | None) -> bool:
    """Only tenant custom (non-default, non-predefined) can be deleted."""
    if not template:
        return False
    if template.get("is_default"):
        return False
    if template.get("source") in ("predefined", "platform"):
        return False
    return True


def update_template(
    tenant_id: str,
    template_id: str,
    **fields: Any,
) -> dict[str, Any] | None:
    """Partial update. Enforces the one-template-per-paper-type rule.

    Raises:
        ValueError("DUPLICATE_PAPER_TYPE_TEMPLATE"): another custom template
            already uses the requested paper-type selection.
    """
    # Default/predefined now editable — no TEMPLATE_NOT_EDITABLE for update
    if "paper_types" in fields and fields["paper_types"] is not None:
        if (
            _find_duplicate_type_template(tenant_id, fields["paper_types"], exclude_id=template_id)
            is not None
        ):
            raise ValueError("DUPLICATE_PAPER_TYPE_TEMPLATE")
    return template_store.update(tenant_id, template_id, **fields)


def delete_template(tenant_id: str, template_id: str) -> bool:
    """Delete only tenant custom; default/predefined not deletable."""
    existing = template_store.get_by_id(tenant_id, template_id)
    if not existing:
        return False
    if not _is_deletable(existing):
        raise ValueError("DEFAULT_TEMPLATE_NOT_DELETABLE")
    return template_store.delete(tenant_id, template_id)


def activate_template(tenant_id: str, template_id: str) -> dict[str, Any] | None:
    return template_store.set_active(tenant_id, template_id, is_active=True)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def save_sections(
    tenant_id: str,
    template_id: str,
    sections: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Persist the section order for a template. Default/predefined now editable."""
    return template_store.update(tenant_id, template_id, sections=sections)


# ---------------------------------------------------------------------------
# Papers
# ---------------------------------------------------------------------------


def list_papers(tenant_id: str, template_id: str) -> list[dict[str, Any]]:
    return template_store.list_papers(tenant_id, template_id)


def get_papers_sections(tenant_id: str, template_id: str) -> list[dict[str, Any]]:
    """Merged section list across the template's papers.

    Same rule as onboarding (upload order, deduped, source-labeled) via the
    shared helper. No voice involved in the template journey.
    """
    from creditpaper.services.onboarding.sections_merge import merge_section_lists

    papers = template_store.list_papers(tenant_id, template_id)
    return merge_section_lists(
        [{"filename": p.get("filename", ""), "sections": p.get("extracted_sections") or []} for p in papers]
    )


def upload_paper(
    tenant_id: str,
    template_id: str,
    *,
    filename: str,
    content: bytes,
    content_type: str | None,
) -> dict[str, Any]:
    """Classify, store, and record a paper for this template.

    Reuses the onboarding document classifier AND section/style extraction
    for paper-type detection. Max 3 papers per template (same as onboarding).
    """
    from pathlib import Path

    from creditpaper.onboarding.services.document_service import store_and_process

    if template_store.count_papers(tenant_id, template_id) >= 3:
        raise ValueError("MAX_DOCUMENTS_REACHED")

    # Use the onboarding classifier to validate the paper
    entry = store_and_process(tenant_id, filename, content, content_type)

    # Reuse the storage_path the onboarding service already wrote to
    storage_path = entry.get("storage_path", "")
    if not storage_path:
        # Fallback: write to a template-specific directory
        templates_dir = Path("output/templates") / tenant_id / template_id
        templates_dir.mkdir(parents=True, exist_ok=True)
        target = templates_dir / filename
        from creditpaper.security.document_storage import write_document

        write_document(
            target,
            content,
            tenant_id=tenant_id,
            record_id=str(entry["id"]),
        )
        storage_path = str(target)

    return template_store.add_paper(
        tenant_id,
        template_id,
        filename=filename,
        storage_path=storage_path,
        is_credit_paper=bool(entry.get("is_credit_paper", False)),
        approval_detected=bool(entry.get("approval_detected", False)),
        confidence=float(entry.get("confidence", 0.0)),
        size=len(content),
        content_type=content_type,
        extracted_sections=entry.get("extracted_sections") or [],
        tags=entry.get("tags") or [],
    )


def delete_paper(tenant_id: str, template_id: str, paper_id: str) -> bool:
    return template_store.delete_paper(tenant_id, template_id, paper_id)


# ---------------------------------------------------------------------------
# Preview .docx
# ---------------------------------------------------------------------------


def render_preview_doc(tenant_id: str, template_id: str) -> bytes | None:
    """Render a sample .docx using the template's brand, sections, and paper types.

    Reuses the rendering logic from onboarding/preview_doc.py. Falls back to
    a minimal placeholder if the rendering helpers are not importable.
    """
    try:
        # download_preview returns a StreamingResponse; extract the body
        # by calling its internals. For a simpler approach, we re-implement
        # the docx build here.
        from io import BytesIO

        from docx import Document
        from docx.shared import RGBColor

        template = template_store.get_by_id(tenant_id, template_id)
        if not template:
            return None

        # Heading colour from the firm's resolved paper theme (platform default when
        # the firm has no brand), not a hard-coded colour.
        from creditpaper.paper.theme import get_theme

        try:
            theme = get_theme(tenant_id, logo_endpoint="/api/paper/logo")
            heading_hex = (theme.get("colours") or {}).get("heading") or "#1F3A5F"
        except Exception:
            heading_hex = "#1F3A5F"
        try:
            heading_rgb = RGBColor.from_string(heading_hex.lstrip("#"))
        except Exception:
            heading_rgb = RGBColor(0x1F, 0x3A, 0x5F)

        # Build a simple docx with template name + sections list
        doc = Document()
        title = doc.add_heading(f"{template['name']} — Preview", level=0)
        if title.runs:
            title.runs[0].font.color.rgb = heading_rgb

        doc.add_paragraph()
        meta = doc.add_paragraph()
        meta.add_run("Template\n").bold = True
        if template.get("paper_types"):
            meta.add_run(f"  Paper types: {', '.join(template['paper_types'])}\n")
        meta.add_run(f"  Source: {template.get('source', 'custom')}\n")

        # Brand-dynamic hierarchy: sections → subsections → sub-subsections (nth) same as preview
        def _add_section_docx(section: dict, level: int = 1, parent_num: str = "") -> None:
            num = section.get("number", parent_num)
            name = section.get("name", "")
            style = "Heading 1" if level == 1 else "Heading 2" if level == 2 else "Heading 3"
            p = doc.add_paragraph(style=style)
            run = p.add_run(f"{num}  {name}")
            # Primary for L1, secondary for L2, accent otherwise — same as preview brand dynamic
            try:
                theme2 = get_theme(tenant_id, logo_endpoint="/api/paper/logo")
                secondary_hex = (theme2.get("colours") or {}).get("secondary") or heading_hex
                accent_hex = (theme2.get("colours") or {}).get("accent") or heading_hex
                sec_rgb = RGBColor.from_string(secondary_hex.lstrip("#"))
                acc_rgb = RGBColor.from_string(accent_hex.lstrip("#"))
            except Exception:
                sec_rgb = heading_rgb
                acc_rgb = heading_rgb
            run.font.color.rgb = heading_rgb if level == 1 else sec_rgb if level == 2 else acc_rgb
            body = doc.add_paragraph()
            body.add_run(
                f"This is a preview of Section {num} ({name}). The content shown here is "
                "placeholder text — a real paper would contain your deal-specific prose, "
                "tables, and citations. The structure, numbering, and colour follow your "
                "house style."
            )
            for sub in section.get("subsections") or []:
                _add_section_docx(sub, level + 1, sub.get("number", ""))

        if template.get("sections"):
            doc.add_paragraph()
            doc.add_paragraph().add_run("Sections (in order)").bold = True
            for s in template["sections"]:
                _add_section_docx(s, level=1)

        buf = BytesIO()
        doc.save(buf)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001
        log.exception("render_preview_doc failed: %s", e)
        return None
