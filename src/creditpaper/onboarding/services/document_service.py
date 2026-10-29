from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path

from creditpaper.onboarding.utils.store import (
    load_documents,
    papers_storage_path,
)

log = logging.getLogger(__name__)

ALLOWED_EXTS = {".pdf", ".docx"}
ALLOWED_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/octet-stream",
}
MAX_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", str(20 * 1024 * 1024)))
MAX_DOCS = 3
REQUIRED_VALIDATED = 1

PDF_MAGIC = b"%PDF"
DOCX_MAGIC = b"PK\x03\x04"

CREDIT_SIGNALS = [
    "borrower",
    "facility",
    "security",
    "lvr",
    "valuation",
    "sponsor",
    "guarantor",
    "recommendation",
    "risk",
    "covenants",
    "credit committee",
    "credit paper",
    "credit_paper",
    "credit",
    "paper",
    "tranche",
    "drawdown",
    "equity",
    "leverage",
    "headroom",
]
APPROVAL_SIGNALS = [
    "approved",
    "credit committee",
    "recommendation: approve",
    "approval date",
    "approved by",
    "conditions of approval",
    "committee decision",
    "decision",
    "signed",
    "signature",
    "digitally signed",
    "digital signature",
    "executed",
    "authorised",
    "authorized",
    "final approval",
    "board approval",
    "management approval",
    "sanctioned",
    "endorsement",
    "endorsed",
]


def _extract_pdf_text(content: bytes) -> str:
    """Extract text from PDF through pypdfium2 (`ingest/reader.py`), which handles CID fonts."""
    try:
        from creditpaper.ingest.reader import PdfiumReader

        with PdfiumReader(content) as reader:
            pages = [reader.page(n).text for n in range(1, reader.page_count() + 1)]
        return " ".join(pages)
    except Exception:
        pass
    try:
        decoded = content.decode("latin-1", errors="ignore")
    except Exception:
        decoded = content.decode("utf-8", errors="ignore")
    printable_pattern = re.compile(
        r"[\x20-\x7e\u00a0-\u00ff"
        r"\u2010-\u2027\u2030-\u205e"
        r"\u2190-\u21ff\u25a0-\u25ff]{6,}"
    )
    matches = printable_pattern.findall(decoded)
    return " ".join(matches)


def extract_plain_text(filename: str, content: bytes) -> str:
    """Unified text layer: PDF via pypdfium2 (no OCR), DOCX via python-docx."""
    try:
        if filename.lower().endswith(".pdf"):
            return _extract_pdf_text(content)
        from creditpaper.services.onboarding.docx_extractor import (
            extract_text as extract_docx_text,
        )

        return extract_docx_text(content)
    except Exception:
        return ""


def backfill_doc_extras(tenant_id: str) -> int:
    """Re-extract sections/style/tags for rows stored before those fields
    were persisted (legacy uploads). Reads the stored files, never the
    network. Returns the number of rows repaired."""
    from creditpaper.onboarding.utils.store import upsert_document

    try:
        from creditpaper.services.onboarding.docx_extractor import (
            extract_sections as extract_docx_sections,
        )
        from creditpaper.services.onboarding.docx_extractor import (
            extract_style as extract_docx_style,
        )
        from creditpaper.services.onboarding.pdf_section_extractor import (
            extract_sections as extract_pdf_sections,
        )
        from creditpaper.services.onboarding.pdf_style_extractor import (
            extract_style as extract_pdf_style,
        )
    except Exception:
        return 0

    fixed = 0
    for d in load_documents(tenant_id):
        if d.get("extracted_sections") and d.get("tags"):
            continue
        sp = d.get("storage_path")
        filename = d.get("filename", "")
        if not sp:
            continue
        try:
            from creditpaper.security.document_storage import read_document

            content = read_document(Path(sp), tenant_id=tenant_id, record_id=str(d["id"]))
        except (OSError, ValueError):
            continue
        text = extract_plain_text(filename, content)
        try:
            if filename.lower().endswith(".pdf"):
                sections = extract_pdf_sections(content) or []
                style = extract_pdf_style(content) or {}
            else:
                sections = extract_docx_sections(content) or []
                style = extract_docx_style(content) or {}
        except Exception:
            sections, style = [], {}
        row = dict(d)
        if not row.get("extracted_sections") and sections:
            row["extracted_sections"] = sections
        if not row.get("extracted_style"):
            row["extracted_style"] = style or {
                "font_heading": None,
                "font_body": None,
                "primary_color": None,
            }
        if not row.get("tags") and text:
            row["tags"] = _extract_tags(text + " " + filename)
        try:
            upsert_document(tenant_id, row)
            fixed += 1
        except Exception:
            log.exception("backfill_doc_extras: upsert failed")
    return fixed


def _detect_mime_and_magic(content: bytes, filename: str) -> tuple[str, bool]:
    ext = Path(filename).suffix.lower()
    if content.startswith(PDF_MAGIC):
        return "application/pdf", ext == ".pdf"
    if content.startswith(DOCX_MAGIC):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ext == ".docx"
    return "unknown", False


def validate_file(filename: str, content: bytes, content_type: str | None = None) -> str | None:
    if not filename or not content:
        return "FILE_CORRUPTED"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return "FILE_TYPE_NOT_SUPPORTED"
    if len(content) > MAX_SIZE:
        return "FILE_TOO_LARGE"
    if len(content) < 10:
        return "FILE_CORRUPTED"
    mime, magic_ok = _detect_mime_and_magic(content, filename)
    if mime == "unknown":
        return "FILE_TYPE_NOT_SUPPORTED"
    if not magic_ok:
        return "FILE_TYPE_NOT_SUPPORTED"
    if content_type and content_type not in ALLOWED_MIMES:
        pass
    return None


def _classify_credit_paper(text: str) -> tuple[bool, float]:
    low = text.lower()
    hits = sum(1 for s in CREDIT_SIGNALS if s in low)
    confidence = min(0.95, hits / 6.0 + 0.1)
    is_credit = hits >= 3
    return is_credit, confidence


def _detect_approval(text: str) -> tuple[bool, float]:
    low = text.lower()
    hits = sum(1 for s in APPROVAL_SIGNALS if s in low)
    confidence = min(0.95, hits / 2.0 + 0.2)
    approval_keywords = ("approv", "sign", "execut", "endors", "sanction")
    detected = hits >= 1 and any(k in low for k in approval_keywords)
    return detected, confidence


def process_document_text(text: str) -> dict:
    is_credit, credit_conf = _classify_credit_paper(text)
    approval_detected, approval_conf = _detect_approval(text)
    if not is_credit:
        return {
            "is_credit_paper": False,
            "approval_detected": False,
            "confidence": credit_conf,
            "status": "rejected",
            "reason": "NOT_A_CREDIT_PAPER",
        }
    if not approval_detected:
        return {
            "is_credit_paper": True,
            "approval_detected": False,
            "confidence": approval_conf,
            "status": "rejected",
            "reason": "APPROVAL_NOT_DETECTED",
        }
    return {
        "is_credit_paper": True,
        "approval_detected": True,
        "confidence": max(credit_conf, approval_conf),
        "status": "validated",
        "reason": None,
    }


def _extract_tags(text: str, *, limit: int = 12) -> list[str]:
    """Real per-document tags: credit + approval signals actually present."""
    low = text.lower()
    tags: list[str] = []
    for sig in CREDIT_SIGNALS:
        if sig in low and sig not in tags:
            tags.append(sig)
    for sig in ("approved", "signed", "sanctioned", "endorsed", "executed"):
        if sig in low and sig not in tags:
            tags.append(sig)
    return tags[:limit]


def store_and_process(
    tenant_id: str,
    filename: str,
    content: bytes,
    content_type: str | None = None,
) -> dict:
    err = validate_file(filename, content, content_type)
    if err:
        raise ValueError(err)

    existing = load_documents(tenant_id)
    same_document = next((d for d in existing if d.get("filename") == filename), None)
    same_name = same_document is not None
    if not same_name and len(existing) >= MAX_DOCS:
        raise ValueError("MAX_DOCUMENTS_REACHED")

    # Filename is the database upsert key, so replacements must retain the
    # existing record ID or their encrypted-file AAD would no longer match.
    doc_id = str(same_document["id"]) if same_document else str(uuid.uuid4())
    storage_path = papers_storage_path(tenant_id, doc_id, filename)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    from creditpaper.security.document_storage import write_document

    write_document(storage_path, content, tenant_id=tenant_id, record_id=doc_id)

    text = extract_plain_text(filename, content)

    classification = process_document_text(text + " " + filename)
    tags = _extract_tags(text + " " + filename)

    extracted_sections: list[dict] = []
    extracted_style: dict = {"font_heading": None, "font_body": None, "primary_color": None}
    try:
        if filename.lower().endswith(".pdf"):
            from creditpaper.services.onboarding.pdf_section_extractor import (
                extract_sections,
            )
            from creditpaper.services.onboarding.pdf_style_extractor import (
                extract_style,
            )
        else:
            from creditpaper.services.onboarding.docx_extractor import (
                extract_sections,
                extract_style,
            )

        extracted_sections = extract_sections(content) or []
        extracted_style = extract_style(content) or extracted_style
    except Exception:
        log.exception("store_and_process: section/style extraction failed")

    from creditpaper.onboarding.utils import store as onboarding_store

    entry = {
        "id": doc_id,
        "filename": filename,
        "status": classification["status"],
        "is_credit_paper": classification["is_credit_paper"],
        "approval_detected": classification["approval_detected"],
        "confidence": classification["confidence"],
        "reason": classification["reason"],
        "size": len(content),
        "content_type": content_type or "unknown",
        "tags": tags,
        "storage_path": str(storage_path),
        "extracted_sections": extracted_sections,
        "extracted_style": extracted_style,
    }
    onboarding_store.upsert_document(tenant_id, entry)

    from creditpaper.onboarding.utils.store import mark_step_completed

    if onboarding_store.validated_count(tenant_id) >= REQUIRED_VALIDATED:
        mark_step_completed(tenant_id, "papers")

    return entry


def list_documents(tenant_id: str) -> list[dict]:
    return load_documents(tenant_id)
