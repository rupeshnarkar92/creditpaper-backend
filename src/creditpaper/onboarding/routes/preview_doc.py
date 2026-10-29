"""Dummy credit paper preview — renders a sample .docx using the tenant's
brand profile, voice profile, and section order. Used on Step 5 of onboarding
so the user can see what their papers will look like before committing.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from docx import Document
from docx.shared import RGBColor
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from creditpaper.api.tenant import resolve_tenant_id
from creditpaper.auth.dependencies import CurrentUser, require_permission
from creditpaper.onboarding.utils.store import (
    load_brand_profile,
    load_section_order,
    load_voice_profile,
    save_section_order,
)
from creditpaper.paper.defaults_store import load_current
from creditpaper.paper.theme import get_theme

router = APIRouter(prefix="/api/onboarding/preview-doc", tags=["onboarding"])


def _hex_to_rgb(h: str) -> RGBColor | None:
    if not h or not h.startswith("#") or len(h) != 7:
        return None
    try:
        return RGBColor(int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16))
    except ValueError:
        return None


SAMPLE_BODY = (
    "The facility will be secured by a first ranking mortgage over the subject site, "
    "supported by a comprehensive security package detailed in the sections that follow. "
    "This paper is a preview generated from the tenant's brand and voice profile to show "
    "how a completed credit paper will look in their house style."
)


def _load_section_order(tenant_id: str) -> list[tuple[str, str]]:
    rows = load_section_order(tenant_id)
    if rows:
        # Whole JSONB column: each row may carry subsections, but preview doc renders flat sections.
        return [(str(s.get("number", "")), str(s.get("name", ""))) for s in rows]
    # No order saved: the Creditpaper Standard template (a stored platform default).
    standard = load_current("template").get("payload") or {}
    return [(str(s.get("number", "")), str(s.get("name", ""))) for s in standard.get("sections") or []]


@router.get("", response_class=StreamingResponse)
def download_preview(
    tenant_id: str = Depends(resolve_tenant_id),
    _perm: CurrentUser = Depends(require_permission("settings.read")),  # noqa: B008
):
    brand = load_brand_profile(tenant_id) or {}
    voice = load_voice_profile(tenant_id) or {}

    # Fallbacks come from the firm's resolved paper theme, which is the platform
    # default theme when the firm has no brand.
    paper_theme = get_theme(tenant_id, logo_endpoint="/api/paper/logo")
    primary = brand.get("brand", {}).get("colors", {}).get("primary") or paper_theme["colours"]["heading"]
    secondary = brand.get("brand", {}).get("colors", {}).get("secondary")
    accent = brand.get("brand", {}).get("colors", {}).get("accent")
    typography = brand.get("brand", {}).get("typography", {})
    heading_font = typography.get("heading") or paper_theme["fonts"]["heading"]["family"]
    body_font = typography.get("body") or paper_theme["fonts"]["body"]["family"]
    firm_name = brand.get("brand", {}).get("name") or "Your Firm"
    tenant_voice_sample = voice.get("samples", {}).get("tenant_voice") or SAMPLE_BODY
    tone = voice.get("style", {}).get("tone") or "analytical"
    register = voice.get("style", {}).get("register") or "formal"
    terminology = voice.get("terminology") or []

    section_order = _load_section_order(tenant_id)

    doc = Document()
    primary_rgb = _hex_to_rgb(primary)
    if primary_rgb:
        for s in doc.sections:
            s.page_height = 297 * 914400 // 25
            s.page_width = 210 * 914400 // 25

    title = doc.add_heading(f"{firm_name} — Credit Paper Preview", level=0)
    if primary_rgb:
        for run in title.runs:
            run.font.color.rgb = primary_rgb

    sub = doc.add_paragraph()
    sub_run = sub.add_run(
        "This is a preview rendered from your brand and voice profile. "
        "Sections appear in your chosen order, in your house style."
    )
    sub_run.italic = True
    sub_run.font.size = None

    doc.add_paragraph()
    meta = doc.add_paragraph()
    meta.add_run("Brand\n").bold = True
    meta.add_run(f"  Primary: {primary}\n")
    if secondary:
        meta.add_run(f"  Secondary: {secondary}\n")
    if accent:
        meta.add_run(f"  Accent: {accent}\n")
    meta.add_run(f"  Heading: {heading_font}   Body: {body_font}\n")

    doc.add_paragraph()
    voice_para = doc.add_paragraph()
    voice_para.add_run("Voice\n").bold = True
    voice_para.add_run(f"  Tone: {tone}   Register: {register}\n")
    voice_para.add_run(f"  Sample: {tenant_voice_sample}\n")

    if terminology:
        doc.add_paragraph()
        term_para = doc.add_paragraph()
        term_para.add_run("Terminology\n").bold = True
        term_para.add_run("  " + ", ".join(terminology))

    # Brand-dynamic hierarchy same as template preview: primary/secondary/accent, nth levels
    secondary_rgb = _hex_to_rgb(secondary) or primary_rgb
    accent_rgb = _hex_to_rgb(accent) or primary_rgb
    doc.add_paragraph()
    doc.add_paragraph().add_run("Sections (in order)").bold = True
    _fallback = (load_current("template").get("payload") or {}).get("sections") or []
    full_sections = load_section_order(tenant_id) or _fallback

    def _render_preview_section(sec: dict, level: int = 1) -> None:
        num = sec.get("number", "")
        name = sec.get("name", "")
        style = "Heading 1" if level == 1 else "Heading 2" if level == 2 else "Heading 3"
        p = doc.add_paragraph(style=style)
        run = p.add_run(f"{num}  {name}")
        if level == 1 and primary_rgb:
            run.font.color.rgb = primary_rgb
        elif level == 2 and secondary_rgb:
            run.font.color.rgb = secondary_rgb
        elif accent_rgb:
            run.font.color.rgb = accent_rgb
        body = doc.add_paragraph()
        body.add_run(
            f"This is a preview of Section {num} ({name}). The content shown here is "
            "placeholder text — a real paper would contain your deal-specific prose, "
            "tables, and citations. The structure, numbering, and colour follow your house style."
        )
        for sub in sec.get("subsections") or []:
            _render_preview_section(sub, level + 1)

    if full_sections:
        for sec in full_sections:
            _render_preview_section(sec, 1)
    else:
        for number, name in section_order:
            p = doc.add_paragraph(style="Heading 1")
            run = p.add_run(f"{number}  {name}")
            if primary_rgb:
                run.font.color.rgb = primary_rgb
            body = doc.add_paragraph()
            body.add_run(
                f"This is a preview of Section {number} ({name}). The content shown here is "
                "placeholder text — a real paper would contain your deal-specific prose, "
                "tables, and citations. The structure, numbering, and colour follow your house style."
            )

    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)

    headers = {"Content-Disposition": 'attachment; filename="credit_paper_preview.docx"'}
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers,
    )


@router.post("/section-order")
def save_section_order_endpoint(
    body: dict[str, Any],
    tenant_id: str = Depends(resolve_tenant_id),
    _admin: CurrentUser = Depends(require_permission("settings.update")),  # noqa: B008
):
    sections = body.get("sections")
    if not isinstance(sections, list) or not sections:
        raise HTTPException(status_code=400, detail="sections must be a non-empty list")
    # Whole JSONB column: validate hierarchy, then persist entire array atomically.
    # Each entry may have subsections: [{number,name,subsections:[{number,name}]}]
    if len(sections) > 20:
        raise HTTPException(status_code=400, detail="A template holds up to 20 sections")
    for s in sections:
        subs = s.get("subsections") or []
        if not isinstance(subs, list):
            raise HTTPException(status_code=400, detail="subsections must be a list")
        if len(subs) > 30:
            raise HTTPException(status_code=400, detail="A section holds up to 30 subsections")
    save_section_order(tenant_id, sections)
    return {"ok": True, "count": len(sections)}
