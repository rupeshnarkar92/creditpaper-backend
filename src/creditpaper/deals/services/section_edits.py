"""Persist the existing paper editor without losing its rich document structure."""

from __future__ import annotations

import json
import re
from typing import Any

from creditpaper.db import tenant_cursor, tenant_uuid
from creditpaper.deals.services.section_resolver import map_sections


class EditError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def editor_prose(value: list[dict[str, Any]]) -> str:
    """Validate Slate nodes and project their text to the renderer's line grammar."""
    count = 0

    text_blocks = {"paragraph", "p", "heading", "subheading", "scaffold", "analyst_action"}

    def node_text(node: Any, depth: int = 0, parent: str | None = None) -> str:
        nonlocal count
        count += 1
        if count > 20000 or depth > 32 or not isinstance(node, dict):
            raise ValueError("Invalid editor document")
        if "text" in node:
            if (
                not isinstance(node["text"], str)
                or "children" in node
                or "type" in node
                or parent not in text_blocks | {"table_cell"}
            ):
                raise ValueError("Editor text must be a string")
            if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", node["text"]):
                raise ValueError("Editor text contains unsupported control characters")
            for mark in ("bold", "italic", "underline", "strikethrough", "code", "highlight"):
                if node.get(mark) is not None and not isinstance(node[mark], bool):
                    raise ValueError("Editor marks must be boolean")
            size = node.get("fontSize")
            if size is not None:
                if isinstance(size, bool) or not isinstance(size, (int, float, str)):
                    raise ValueError("Invalid editor font size")
                if isinstance(size, str):
                    if not re.fullmatch(r"\d+(?:\.\d+)?(?:px)?", size):
                        raise ValueError("Invalid editor font size")
                    size = float(size.removesuffix("px"))
                if not 8 <= size <= 72:
                    raise ValueError("Editor font size must be between 8 and 72 pixels")
            return node["text"]
        kind = node.get("type")
        if not isinstance(kind, str) or kind not in text_blocks | {"table", "table_row", "table_cell"}:
            raise ValueError("Unsupported editor element")
        allowed = (
            text_blocks | {"table"}
            if parent is None
            else {"table_row"}
            if parent == "table"
            else {"table_cell"}
            if parent == "table_row"
            else text_blocks
            if parent == "table_cell"
            else set()
        )
        if kind not in allowed:
            raise ValueError("Invalid editor element nesting")
        align = node.get("align")
        if align is not None and (
            not isinstance(align, str) or align not in {"left", "center", "right", "justify"}
        ):
            raise ValueError("Invalid editor alignment")
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise ValueError("Editor elements need children")
        if kind == "table_cell" and any(isinstance(c, dict) and "text" in c for c in children):
            if not all(isinstance(c, dict) and "text" in c for c in children):
                raise ValueError("Table cells cannot mix text and block children")
        parts = [node_text(child, depth + 1, kind) for child in children]
        if kind == "table":
            return "\n".join(parts)
        if kind == "table_row":
            return "| " + " | ".join(parts) + " |"
        text = "".join(parts)
        return {
            "heading": "## ",
            "subheading": "### ",
            "scaffold": ">> ",
            "analyst_action": "[ANALYST] ",
        }.get(kind, "") + text

    if not isinstance(value, list) or not value:
        raise ValueError("Editor document is empty or too large")
    prose = "\n\n".join(node_text(node) for node in value)
    try:
        encoded = json.dumps(value, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError("Invalid editor document") from None
    if len(encoded) > 2_000_000:
        raise ValueError("Editor document is empty or too large")
    return prose


def save_section(
    tenant_id: str, deal_id: str, key: str, actor_id: str, value: list[dict[str, Any]], expected_revision: int
) -> dict:
    prose = editor_prose(value)
    tid = str(tenant_uuid(tenant_id))
    with tenant_cursor(tid) as cur:
        cur.execute(
            "SELECT current_state,template_snapshot FROM deals WHERE tenant_id=%s AND id=%s FOR UPDATE",
            (tid, deal_id),
        )
        deal = cur.fetchone()
        if not deal:
            raise EditError("DEAL_NOT_FOUND", "Deal not found", 404)
        if deal[0] in {"APPROVED", "ISSUED", "ARCHIVED"}:
            raise EditError(
                "PAPER_LOCKED", "Approved or archived papers cannot be edited; reopen review first"
            )
        engine = map_sections((deal[1] or {}).get("sections", [])).get(key)
        if engine is None:
            raise EditError("SECTION_NOT_WRITABLE", "This section has no editable paper content")
        cur.execute(
            "SELECT 1 FROM processing_jobs WHERE tenant_id=%s AND deal_id=%s "
            "AND kind='generation' AND status IN ('pending','running') LIMIT 1",
            (tid, deal_id),
        )
        if cur.fetchone():
            raise EditError("GENERATION_IN_PROGRESS", "Wait for paper generation before editing")
        cur.execute(
            "SELECT public.save_edited_paper_section(%s::uuid, %s::text, %s::integer, "
            "%s::integer, %s::jsonb, %s::text, %s::uuid)",
            (tid, deal_id, engine, expected_revision, json.dumps(value), prose, actor_id),
        )
        row = cur.fetchone()
        revision = row[0] if row else None
        if revision is None:
            raise EditError("SECTION_REVISION_MISMATCH", "Paper changed; reload before saving this edit")
        updated = (revision,)
    return {"key": key, "editor_value": value, "prose": prose, "edit_revision": updated[0]}
