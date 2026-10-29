"""Structural parse of generated section prose for the review UI.

Splits the line grammar the agents emit (`creditpaper.markup`) into typed blocks
a rich-text editor can render distinctly.

This used to be a SECOND parser for that grammar, and it had drifted from the
renderer's: it knew `## `, `>> ` and `[ANALYST] ` only, so `### ` subheadings and
markdown pipe tables fell through to `paragraph`. A reviewer opening Section 6
saw raw `|---|` markup, and Section 7's structure was invisible in the very
surface built for reviewing it. Same text, two answers, no way to notice.

Both sides now classify through `markup.classify_line`. This module still decides
what the EDITOR does with each kind — that is presentation, and it may legitimately
differ from the docx renderer — but it can no longer disagree about what a line IS.
"""

from __future__ import annotations

from typing import Literal

# `typing_extensions`, not `typing`: pydantic rejects `typing.TypedDict` on Python
# < 3.12, because those versions cannot report whether a field is required. This
# type crosses the API boundary via FastAPI, so it is a pydantic model in practice
# and the distinction is load-bearing rather than stylistic.
from typing_extensions import TypedDict

from creditpaper.markup import LineKind, classify_line  # the published line grammar

BlockType = Literal["heading", "subheading", "analyst_action", "scaffold", "table_row", "paragraph"]

# The editor's name for each line kind. Explicit rather than `kind.value` so that
# adding a kind to the grammar is a deliberate decision here, not a silent new
# block type appearing in the API response.
_BLOCK_TYPES: dict[LineKind, BlockType] = {
    LineKind.HEADING: "heading",
    LineKind.SUBHEADING: "subheading",
    LineKind.SCAFFOLD: "scaffold",
    LineKind.ANALYST_ACTION: "analyst_action",
    LineKind.TABLE_ROW: "table_row",
    LineKind.PARAGRAPH: "paragraph",
}


class ProseBlockOut(TypedDict):
    type: BlockType
    text: str


def parse_prose_blocks(text: str) -> list[ProseBlockOut]:
    blocks: list[ProseBlockOut] = []
    for raw_line in text.split("\n"):
        kind, body = classify_line(raw_line)
        if kind is LineKind.BLANK:
            continue
        blocks.append({"type": _BLOCK_TYPES[kind], "text": body})
    return blocks
