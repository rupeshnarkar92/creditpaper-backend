"""Read-time text for deal activity (contract 2026-09-15, cloned from Ceralinq `build_action_text`).

Stored events are structured (slug + metadata); this module turns one into what the Activity
list shows. Pure: no database, no clock. Labels are plain, specific, sentence case, and never
show a raw slug or JSON; an unknown slug is humanised.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

PLATFORM_ACTOR = "Creditpaper"

CATEGORIES = ("documents", "facts", "paper", "stage", "deal")

_CATEGORY_BY_PREFIX = (
    ("document.", "documents"),
    ("document_slot.", "documents"),
    ("document_slots.", "documents"),
    ("extraction.", "documents"),
    ("fact.", "facts"),
    ("paper.", "paper"),
    ("template.", "paper"),
    ("state.", "stage"),
    ("deal.", "deal"),
)

STATE_LABELS = {
    "DRAFT": "Draft",
    "INTAKE": "Intake",
    "PROCESSING": "Processing",
    "FACT_REVIEW": "Fact Review",
    "PAPER_GENERATED": "Paper Generated",
    "PAPER_EDITING": "Paper Editing",
    "PAPER_REVIEW": "Paper Review",
    "PENDING_APPROVAL": "Pending Approval",
    "APPROVED": "Approved",
    "REJECTED": "Rejected",
    "ISSUED": "Issued",
    "ARCHIVED": "Archived",
}

_POSITIVE = {
    "fact.approved",
    "document_slot.confirmed",
    "paper.section_generated",
    "paper.recommendation_signed",
}
_NEGATIVE = {
    "extraction.failed",
    "fact.rejected",
    "document.withdrawn",
    "paper.section_failed",
    "paper.generation_failed",
    "deal.deleted",
}
_ENGINE = {"extraction.completed", "document_slots.proposed"}


def category_for(action: str) -> str:
    for prefix, category in _CATEGORY_BY_PREFIX:
        if action.startswith(prefix):
            return category
    return "deal"


def _int(value: object) -> int:
    if isinstance(value, int | float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def tone_for(action: str, metadata: dict[str, Any]) -> str:
    if action == "paper.generated":
        return "negative" if _int(metadata.get("failed_count")) > 0 else "positive"
    if action == "state.transition":
        to = metadata.get("to")
        if to == "APPROVED":
            return "positive"
        if to == "REJECTED":
            return "negative"
        return "neutral"
    if action in _POSITIVE:
        return "positive"
    if action in _NEGATIVE:
        return "negative"
    if action in _ENGINE:
        return "engine"
    return "neutral"


def state_label(state: object) -> str:
    text = str(state or "")
    return STATE_LABELS.get(text) or text.replace("_", " ").title() or "a new stage"


def humanise_slug(action: str) -> str:
    words = action.replace(".", " ").replace("_", " ").split()
    return " ".join(words).capitalize() if words else "Deal updated"


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _document(md: dict[str, Any]) -> str:
    name = _text(md.get("document_name")) or _text(md.get("display_name")) or _text(md.get("filename"))
    if name:
        return name
    names = [n for n in (md.get("document_names") or md.get("filenames") or []) if _text(n)]
    if len(names) == 1:
        return str(names[0])
    if names:
        return f"{len(names)} documents"
    ids = md.get("document_ids") or []
    if isinstance(ids, list) and len(ids) > 1:
        return f"{len(ids)} documents"
    return "Document"


def _slot(md: dict[str, Any]) -> str:
    name = _text(md.get("slot_name"))
    if name:
        return name
    key = _text(md.get("slot_key"))
    return key.replace("_", " ").replace("-", " ").capitalize() if key else "its slot"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def label_for(action: str, metadata: dict[str, Any] | None) -> str:
    md = metadata or {}
    if action == "deal.created":
        name = _text(md.get("name"))
        return f"Deal created: {name}" if name else "Deal created"
    if action == "deal.deleted":
        name = _text(md.get("name"))
        return f"Deal deleted: {name}" if name else "Deal deleted"
    if action == "document.uploaded":
        return f"Document uploaded: {_document(md)}"
    if action == "document.withdrawn":
        return f"Document withdrawn: {_document(md)}"
    if action == "document_slot.confirmed":
        return f"{_document(md)} confirmed as {_slot(md)}"
    if action == "document_slot.assigned":
        return f"{_document(md)} assigned to {_slot(md)}"
    if action == "document_slot.changed":
        return f"{_document(md)} chosen for {_slot(md)}"
    if action == "document_slot.unassigned":
        return f"{_document(md)} removed from {_slot(md)}"
    if action == "document_slot.retried":
        return f"Extraction retried: {_slot(md)}"
    if action == "document_slots.proposed":
        n = _int(md.get("proposed_files"))
        return f"{_plural(n, 'document')} matched to slots" if n else "Documents matched to slots"
    if action == "extraction.completed":
        return f"{_plural(_int(md.get('facts_count')), 'fact')} extracted from {_document(md)}"
    if action == "extraction.failed":
        return f"Extraction failed: {_document(md)}"
    if action in ("fact.approved", "fact.rejected"):
        label = _text(md.get("label")) or _text(md.get("path")) or "a fact"
        if action == "fact.rejected":
            return f"Fact rejected: {label}"
        return f"Fact corrected: {label}" if md.get("corrected") else f"Fact approved: {label}"
    if action == "paper.generated":
        text = f"Credit paper generated: {_plural(_int(md.get('sections_count')), 'section')}"
        failed = _int(md.get("failed_count"))
        return f"{text}, {failed} failed" if failed > 0 else text
    if action == "paper.generation_failed":
        return "Credit paper generation failed"
    if action == "paper.section_generated":
        return f"Section regenerated: {_text(md.get('title')) or 'section'}"
    if action == "paper.section_edited":
        return f"Section edited: {_text(md.get('title')) or 'section'}"
    if action == "paper.section_failed":
        return f"Section failed: {_text(md.get('title')) or 'section'}"
    if action == "paper.recommendation_signed":
        author = _text(md.get("author"))
        return f"Recommendation signed by {author}" if author else "Recommendation signed"
    if action == "paper.exported":
        return "Paper exported to Word"
    if action == "state.transition":
        text = f"Moved to {state_label(md.get('to'))}"
        reason = _text(md.get("reason"))
        return f"{text}: {reason}" if reason else text
    if action == "template.changed":
        before = (md.get("from") or {}).get("name") if isinstance(md.get("from"), dict) else None
        after = (md.get("to") or {}).get("name") if isinstance(md.get("to"), dict) else None
        if before and after:
            return f"Template changed from {before} to {after}"
        return f"Template changed to {after}" if after else "Template changed"
    return humanise_slug(action)


def iso_utc(value: object) -> str | None:
    """`2026-09-15T04:32:10Z`. Naive datetimes are taken as UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if not isinstance(value, datetime):
        return str(value)
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def actor_for(actor_id: object, actor_names: dict[str, str]) -> str:
    if not actor_id:
        return PLATFORM_ACTOR
    return actor_names.get(str(actor_id)) or PLATFORM_ACTOR


def present(event: dict[str, Any], actor_names: dict[str, str]) -> dict[str, Any]:
    action = str(event.get("action") or "")
    metadata = event.get("metadata")
    md = metadata if isinstance(metadata, dict) else {}
    return {
        "id": str(event.get("id")),
        "action": action,
        "category": category_for(action),
        "tone": tone_for(action, md),
        "label": label_for(action, md),
        "actor": actor_for(event.get("actor_id"), actor_names),
        "created_at": iso_utc(event.get("created_at")),
    }
