from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from creditpaper.onboarding.utils.store import (
    load_brand_profile,
    load_documents,
    load_voice_profile,
    save_voice_profile,
    validated_count,
)

VOICE_MAX_RETRIES = int(os.getenv("VOICE_GENERATION_MAX_RETRIES", "2"))
VOICE_CORPUS_MAX = int(os.getenv("VOICE_CORPUS_MAX_CHARS", "12000"))


def _load_section_corpus(
    tenant_id: str, max_chars: int = VOICE_CORPUS_MAX
) -> tuple[str, list[dict[str, Any]]]:
    """Build the voice input from stored `extracted_sections` (name + preview
    per section), concatenated across every validated paper with a per-paper
    delimiter. Budget split evenly so no paper is silently cut.

    A paper with no stored sections falls back to its raw file text.
    Returns (corpus, sources) with per-paper char contributions.
    """
    from creditpaper.onboarding.services.document_service import extract_plain_text

    docs = load_documents(tenant_id)
    validated = [d for d in docs if d.get("status") == "validated" and d.get("approval_detected")]
    if not validated:
        return "", []
    per_doc = max(1000, max_chars // len(validated))
    chunks: list[str] = []
    sources: list[dict[str, Any]] = []
    for d in validated:
        filename = d.get("filename", "unknown")
        parts: list[str] = []
        for s in d.get("extracted_sections") or []:
            name = (s.get("name") or "").strip()
            preview = (s.get("preview") or "").strip()
            if name or preview:
                parts.append(f"## {s.get('number', '')} {name}\n{preview}".strip())
        if not parts:
            sp = d.get("storage_path")
            if sp and Path(sp).exists():
                try:
                    from creditpaper.security.document_storage import read_document

                    content = read_document(Path(sp), tenant_id=tenant_id, record_id=str(d["id"]))
                    text = extract_plain_text(filename, content).strip()
                except (OSError, ValueError):
                    text = ""
                if text:
                    parts.append(text)
        if not parts:
            continue
        chunk = "\n\n".join(parts)[:per_doc]
        chunks.append(f"[PAPER: {filename}]\n{chunk}")
        sources.append({"document_id": d.get("id"), "filename": d.get("filename"), "chars": len(chunk)})
    return "\n\n".join(chunks), sources


def _llm_extract_voice(corpus: str, brand_vocab: list[str]) -> dict[str, Any]:
    """Send the corpus to the LLM and get back structure/terminology/tone/samples."""
    from creditpaper.config import prose_model
    from creditpaper.llm.client import messages_create

    system = (
        "You study a single firm's prior approved credit papers to extract their WRITING PATTERNS. "
        "Return ONLY a JSON object with these fields: "
        "structure (array of 4-6 short phrases like 'lead with facility', 'state purpose'), "
        "terminology (array of 6-10 short phrases the firm uses like 'headroom', 'leverage covenant'), "
        "tone (short label: analytical, formal, direct, institutional, etc.), "
        "register (short label: formal, semi-formal, plain), "
        "sentence_length (short label: short, medium, long), "
        "learnings (array of 4-6 short observations about HOW the firm writes), "
        "tenant_voice (rewrite the first substantial section from the corpus in the firm's voice — "
        "KEEP the same factual content (numbers, names, dates, places) but mirror the firm's tone, "
        "sentence shape, and terminology; this should read like the firm actually wrote it), "
        "generic_voice (rewrite the SAME section topic in GENERIC plain AI style — STRIP every "
        "specific number, name, date, and place; replace firm vocabulary with generic placeholders; "
        "use vague qualitative phrasing like 'the borrower is seeking funding', 'the terms are within "
        "policy', 'security will be in place'; the rewrite must look obviously different from "
        "tenant_voice — bland, longer sentences, no concrete details). "
        "Rules: PRINCIPLES not specifics. Do not copy the firm's deal facts verbatim into generic_voice. "
        "Brand vocabulary to favour: " + ", ".join(brand_vocab or []) + "."
    )
    user = (
        "Here is text extracted from the firm's prior approved credit papers. "
        "Study how they write — structure, vocabulary, tone, sentence shape, recurring moves.\n\n"
        f"{corpus}\n\n"
        "Return ONLY the JSON object described in the system prompt."
    )

    try:
        resp = messages_create(
            tool="onboarding:voice",
            stage="onboarding",
            model=prose_model(),
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        raw = resp.text
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return {}
        return json.loads(m.group(0))
    except Exception:
        return {}


def _validate_no_real_entities(text: str) -> str | None:
    if re.search(r"\b\d{1,3}\s+Example Road\b", text):
        return "contains example address"
    if re.search(r"\bJohn Smith\b", text, re.I):
        return "contains real name"
    return None


def _extract_terminology_from_corpus(corpus: str) -> list[str]:
    """Heuristic: pick domain-relevant terms that appear more than once in the corpus."""
    domain_terms = [
        "facility",
        "sponsor",
        "security",
        "lvr",
        "ltv",
        "headroom",
        "covenant",
        "leverage",
        "equity",
        "valuation",
        "tranche",
        "drawdown",
        "loan-to-value",
        "loan to value",
        "icr",
        "dscr",
        "interest cover",
        "first mortgage",
        "first ranking",
        "general security",
        "credit committee",
        "approved",
        "approval",
        "recommendation",
        "construction",
        "completion",
        "pre-sales",
        "presales",
        "loan amount",
        "term",
        "maturity",
        "expiry",
        "security package",
        "guarantor",
        "guarantee",
        "deed",
        "caveat",
        "title",
        "zoning",
        "da",
        "qs report",
        "valuation report",
        "as if complete",
        "as_is_complete",
    ]
    low = corpus.lower()
    found = []
    for term in domain_terms:
        if low.count(term) >= 2:
            found.append(term)
    return found[:12]


def _extract_recurring_phrases(corpus: str, n: int = 5) -> list[str]:
    """Heuristic: pull short recurring phrases that hint at the firm's structure."""
    low = corpus.lower()
    candidates = [
        "the facility",
        "the sponsor",
        "the security",
        "the valuation",
        "loan-to-value",
        "as if complete",
        "first ranking",
        "credit committee",
        "approved by",
        "subject to",
        "on completion",
        "at funding",
        "at practical completion",
    ]
    found = [c for c in candidates if c in low]
    return found[:n]


def _too_similar(a: str, b: str, threshold: float = 0.70) -> bool:
    """True if the two strings share too many words — the LLM didn't actually rewrite."""
    if not a or not b:
        return False
    wa = {w.lower() for w in a.split() if len(w) > 3}
    wb = {w.lower() for w in b.split() if len(w) > 3}
    if not wa or not wb:
        return False
    overlap = len(wa & wb)
    smaller = min(len(wa), len(wb))
    return (overlap / smaller) > threshold


def _generic_from_section(section_text: str) -> str:
    """Heuristic: rewrite a real section into a generic AI-sounding version.

    Goal: maximum contrast with the firm voice. Strip every concrete detail
    (numbers, names, dates, places), collapse to a topic summary, then re-emit
    in bland qualitative phrasing. Falls back to a fixed template if the
    input has no recognisable content.
    """
    text = section_text.strip()
    if not text:
        return ""
    import re as _re

    # Detect the dominant topic from the first sentence.
    first_sentence = _re.split(r"[.!?]\s+", text, maxsplit=1)[0].lower()
    topic_tokens: list[str] = []
    if "facility" in first_sentence or "loan" in first_sentence or "funding" in first_sentence:
        topic_tokens.append("funding arrangement")
    if "construction" in first_sentence or "build" in first_sentence or "develop" in first_sentence:
        topic_tokens.append("construction project")
    if "residential" in first_sentence or "property" in first_sentence or "site" in first_sentence:
        topic_tokens.append("property development")
    if "refinance" in first_sentence or "refinancing" in first_sentence:
        topic_tokens.append("refinancing of existing debt")
    if "guarantee" in first_sentence:
        topic_tokens.append("personal guarantee from the sponsor")
    if not topic_tokens:
        topic_tokens.append("credit request")

    # Build three or four sentences that read like a generic AI summary.
    sentences: list[str] = [
        f"The borrower is seeking a {topic_tokens[0]} for a "
        f"{topic_tokens[1] if len(topic_tokens) > 1 else 'project in a suitable location'}."
    ]
    if len(topic_tokens) > 1:
        sentences.append(
            "The proposed structure appears consistent with the lender's standard policy "
            "parameters for this asset class."
        )
    sentences.extend(
        [
            "The borrower has prior experience delivering comparable projects, and the broader "
            "market context is considered supportive.",
            "Security will be taken over the underlying property, supported by personal "
            "guarantees from the principals of the borrowing entity.",
            "Further detail on the financial analysis, valuation, and risk mitigants is set out "
            "in the sections that follow.",
        ]
    )

    return " ".join(sentences)


def _load_best_section_per_paper(tenant_id: str) -> list[tuple[str, str, str, str]]:
    """Best (longest-preview) section PER validated paper.

    Returns [(document_id, filename, section_name, body), ...] in upload
    order. All papers contribute — no single-paper preference.
    """
    try:
        import json as _json

        from creditpaper.db import tenant_cursor, tenant_uuid

        db_tenant = _resolve_db_tenant(tenant_id)
        with tenant_cursor(db_tenant) as cur:
            cur.execute(
                "SELECT * FROM public.validated_onboarding_documents(%s,TRUE)",
                (tenant_uuid(db_tenant),),
            )
            rows = cur.fetchall()
            out: list[tuple[str, str, str, str]] = []
            for row in rows:
                doc_id, filename, secs = str(row[0]), row[1], row[2]
                if isinstance(secs, str):
                    secs = _json.loads(secs)
                best_name, best_body = "", ""
                for s in secs or []:
                    body = (s.get("preview") or s.get("body") or "").strip()
                    if body and len(body) > len(best_body):
                        best_body = body
                        best_name = s.get("name", "Section")
                if best_body:
                    out.append((doc_id, filename, best_name, best_body))
            return out
    except Exception:
        return []
    return []


def _resolve_db_tenant(tenant_id: str) -> str:
    return str(tenant_id)


def _combined_sample(
    per_paper: list[tuple[str, str, str, str]],
) -> tuple[str, str]:
    """Build combined samples from every paper's best section.

    tenant_voice joins each paper's best body (capped); generic_voice is the
    bland rewrite of the same combined text. Empty strings when no bodies.
    """
    bodies = [b.strip()[:600].rstrip() for _, _, _, b in per_paper if (b or "").strip()]
    tenant_voice = "\n\n".join(bodies)[:1200].rstrip()
    generic_voice = _generic_from_section("\n\n".join(bodies)) if bodies else ""
    if len(generic_voice) > 600:
        generic_voice = generic_voice[:600].rstrip()
    return tenant_voice, generic_voice


def _fallback_profile(
    tenant_id: str, brand_vocab: list[str], corpus: str = "", sources: list[dict] | None = None
) -> dict[str, Any]:
    """Heuristic profile when the LLM is unavailable. Built ONLY from the
    tenant's real papers — terminology/structure from the corpus, samples
    from the longest real section body.

    Raises:
        ValueError("VOICE_NO_CONTENT"): nothing real to learn from.
    """
    if not corpus or len(corpus) < 200:
        raise ValueError("VOICE_NO_CONTENT")
    terminology = _extract_terminology_from_corpus(corpus)
    if not terminology:
        terminology = brand_vocab or ["facility", "sponsor", "security", "headroom"]
    structure = _extract_recurring_phrases(corpus, n=5)
    if not structure:
        structure = ["lead with facility", "state purpose", "quantify sponsor equity", "state leverage"]

    section_preview = _load_best_section_per_paper(tenant_id)
    if not section_preview:
        raise ValueError("VOICE_NO_CONTENT")
    tenant_voice, generic_voice = _combined_sample(section_preview)
    if not tenant_voice or not generic_voice:
        raise ValueError("VOICE_NO_CONTENT")
    section_name = section_preview[0][2]
    if section_name:
        structure = [f"lead with {section_name.lower()}", *structure[:3]]

    per_file_tags: list[dict[str, Any]] = []
    for d in load_documents(tenant_id):
        if d.get("status") == "validated" and d.get("approval_detected") and d.get("tags"):
            per_file_tags.append({"filename": d.get("filename"), "tags": list(d.get("tags") or [])})

    return {
        "section": "deal_summary",
        "voice_version": 2,
        "structure": structure,
        "style": {"sentence_length": "medium", "register": "formal", "tone": "analytical"},
        "terminology": terminology,
        "rules": ["avoid promotional language", "lead with analytical conclusion"],
        "source_papers_count": validated_count(tenant_id),
        "sources": sources or [],
        "per_file_tags": per_file_tags,
        "learnings": [
            f"recurring structure: {' | '.join(structure[:3])}",
            f"recurring vocabulary: {', '.join(terminology[:4])}",
            "uses credit-committee approval language",
            "quantifies leverage constraints",
        ],
        "samples": {
            "generic": generic_voice,
            "tenant_voice": tenant_voice,
            "synthetic_deal": {
                "borrower": "Synthetic Borrower Pty Ltd",
                "facility": "$7,500,000",
                "security": "First ranking mortgage",
                "is_synthetic": True,
            },
        },
    }


def generate_voice_profile(tenant_id: str) -> dict[str, Any]:
    if validated_count(tenant_id) < 1:
        raise ValueError("NEED_VALIDATED_PAPER")

    # Delete-then-insert: a regenerate never inherits keys from the old row.
    from creditpaper.onboarding.utils.store import delete_voice_profile

    delete_voice_profile(tenant_id)

    # Heal rows stored before sections/tags were persisted.
    from creditpaper.onboarding.services.document_service import backfill_doc_extras

    backfill_doc_extras(tenant_id)

    brand = load_brand_profile(tenant_id)
    brand_vocab: list[str] = []
    if brand and brand.get("brand", {}).get("vocabulary"):
        brand_vocab = list(brand["brand"]["vocabulary"][:12])

    corpus, sources = _load_section_corpus(tenant_id)
    if not corpus or len(corpus) < 200:
        return save_voice_profile(tenant_id, _fallback_profile(tenant_id, brand_vocab, corpus, sources))

    extracted = _llm_extract_voice(corpus, brand_vocab)
    if not extracted:
        return save_voice_profile(tenant_id, _fallback_profile(tenant_id, brand_vocab, corpus, sources))

    structure = extracted.get("structure") or ["lead with facility", "state purpose"]
    terminology = extracted.get("terminology") or []
    # Union of per-document tags so every paper's vocabulary feeds the firm voice.
    per_file_tags: list[dict[str, Any]] = []
    for d in load_documents(tenant_id):
        if d.get("status") == "validated" and d.get("approval_detected"):
            file_tags = list(d.get("tags") or [])
            if file_tags:
                per_file_tags.append({"filename": d.get("filename"), "tags": file_tags})
            for t in file_tags:
                if t not in terminology:
                    terminology.append(t)
    for t in brand_vocab:
        if t not in terminology:
            terminology.append(t)
    if not terminology:
        terminology = ["facility", "sponsor"]
    tone = (extracted.get("tone") or "analytical").strip()
    register = (extracted.get("register") or "formal").strip()
    sentence_length = (extracted.get("sentence_length") or "medium").strip()
    learnings = extracted.get("learnings") or []
    tenant_voice = (extracted.get("tenant_voice") or "").strip()
    generic_voice = (extracted.get("generic_voice") or "").strip()

    section_preview = _load_best_section_per_paper(tenant_id)
    if section_preview:
        combined_tenant, combined_generic = _combined_sample(section_preview)
        if not tenant_voice or _validate_no_real_entities(tenant_voice):
            tenant_voice = combined_tenant
        if not generic_voice or _too_similar(generic_voice, tenant_voice):
            # LLM didn't actually rewrite the section; build a generic version
            # from the combined section bodies so the contrast is obvious.
            generic_voice = combined_generic
    if not tenant_voice or not generic_voice:
        raise ValueError("VOICE_NO_CONTENT")

    profile: dict[str, Any] = {
        "section": "deal_summary",
        "voice_version": 2,
        "structure": structure[:8],
        "style": {"sentence_length": sentence_length, "register": register, "tone": tone},
        "terminology": terminology[:12],
        "rules": ["avoid promotional language", "lead with analytical conclusion", "quantify constraints"],
        "source_papers_count": validated_count(tenant_id),
        "sources": sources,
        "per_file_tags": per_file_tags,
        "learnings": learnings[:8],
        "samples": {
            "generic": generic_voice,
            "tenant_voice": tenant_voice,
            "synthetic_deal": {
                "borrower": "Synthetic Borrower Pty Ltd",
                "facility": "$7,500,000",
                "security": "First ranking mortgage",
                "is_synthetic": True,
            },
        },
    }

    return save_voice_profile(tenant_id, profile)


def get_voice_profile(tenant_id: str) -> dict[str, Any] | None:
    return load_voice_profile(tenant_id)


def get_section_preview(tenant_id: str) -> dict[str, Any]:
    brand = load_brand_profile(tenant_id)
    voice = load_voice_profile(tenant_id)

    # Fallbacks are the current default paper theme (a stored platform default), not
    # constants. `default_theme_*` return None only when the platform record cannot be
    # read; the preview then carries no styling value rather than an invented one.
    from creditpaper.paper.theme import default_theme_colour, default_theme_fonts

    default_fonts = default_theme_fonts() or {"heading": None, "body": None}
    default_accent = default_theme_colour("accent")
    colors = {"primary": default_theme_colour("heading"), "secondary": None, "accent": None}
    typography = dict(default_fonts)
    if brand and brand.get("brand"):
        b = brand["brand"]
        if b.get("colors"):
            colors = {
                "primary": b["colors"].get("primary", colors["primary"]),
                "secondary": b["colors"].get("secondary"),
                "accent": b["colors"].get("accent"),
            }
        if b.get("typography"):
            typography = b["typography"]

    # Sections MERGED across every validated paper, in upload order,
    # deduped by normalized name. All papers are equal — no primary.
    doc_sections: list[dict] = []
    try:
        import json as _json

        from creditpaper.db import tenant_cursor, tenant_uuid

        db_tenant = _resolve_db_tenant(tenant_id)
        with tenant_cursor(db_tenant) as cur:
            cur.execute(
                "SELECT * FROM public.validated_onboarding_documents(%s,FALSE)",
                (tenant_uuid(db_tenant),),
            )
            for row in cur.fetchall():
                secs = row[2] or []
                if isinstance(secs, str):
                    try:
                        secs = _json.loads(secs)
                    except Exception:
                        secs = []
                doc_sections.append(
                    {
                        "document_id": str(row[0]),
                        "filename": row[1],
                        "sections": [
                            {"number": s.get("number", f"{i + 1:02d}"), "name": s.get("name", "")}
                            for i, s in enumerate(secs or [])
                        ],
                        "raw": list(secs or []),
                    }
                )
    except Exception:
        pass

    from creditpaper.services.onboarding.sections_merge import merge_section_lists

    merged = merge_section_lists(
        [{"filename": d["filename"], "sections": d["sections"]} for d in doc_sections]
    )
    # Raw bodies aligned with merged rows for preview sampling.
    raw_by_key: dict[str, dict] = {}
    for d in doc_sections:
        for i, s in enumerate(d["sections"]):
            key = re.sub(r"\s+", " ", (s.get("name") or "").strip().lower())
            if key and key not in raw_by_key:
                raw = d["raw"]
                raw_by_key[key] = raw[i] if i < len(raw) else {"name": s.get("name", "")}
    merged_raw = [raw_by_key[re.sub(r"\s+", " ", (m["name"] or "").strip().lower())] for m in merged]

    real_sections: list[dict] = merged_raw
    paper_filename: str | None = doc_sections[0]["filename"] if doc_sections else None

    if merged:
        sections = [
            {"number": s["number"], "name": s["name"], "source_filename": s["source_filename"]}
            for s in merged
        ]
        first_with_body = next(
            (s for s in real_sections if (s.get("preview") or "").strip()),
            real_sections[0] if real_sections else None,
        )
        if first_with_body is None:
            preview_section = {
                "number": "01",
                "title": sections[0]["name"] if sections else "Section",
                "heading": sections[0]["name"] if sections else "Section",
                "body": "(no preview text extracted)",
                "features": [],
            }
        else:
            preview_section = {
                "number": first_with_body.get("number", "01"),
                "title": first_with_body.get("name", "Section"),
                "heading": first_with_body.get("name", "Section"),
                "body": (first_with_body.get("preview") or "").strip(),
                "features": [],
            }
    else:
        # No real sections anywhere: return empties, never invented data.
        # The frontend renders an empty state pointing back to Step 2.
        sections = []
        preview_section = {
            "number": "",
            "title": "",
            "heading": "",
            "body": "",
            "features": [],
        }

    voice_out = voice or {}

    return {
        "sections": sections,
        "preview": preview_section,
        "paper_filename": paper_filename,
        "styling": {
            "heading_font": typography.get("heading") or default_fonts["heading"],
            "body_font": typography.get("body") or default_fonts["body"],
            "primary_color": colors.get("primary") or default_theme_colour("heading"),
            "secondary_color": colors.get("secondary") or default_accent,
            "accent_color": colors.get("accent") or default_accent,
        },
        "brand": brand,
        "voice": voice_out,
    }
