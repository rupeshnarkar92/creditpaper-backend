"""Ingest an analyst's Word comments from a rendered credit paper.

The review loop (and later the Word add-in / cockpit) feeds on this: read each
comment, the exact text it is anchored to, and the section it falls in, so
feedback can be acted on precisely and — per the learning design — classified
into one-off edits vs permanent config (constitution / paper spec / house style).

Reads the .docx directly (word/document.xml comment ranges + the comments part),
so it works regardless of how the comments were authored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text_of(el) -> str:
    return "".join(t.text or "" for t in el.iter(f"{_W}t"))


@dataclass
class ReviewComment:
    comment_id: str
    author: str
    text: str  # the analyst's comment
    anchored_text: str  # the paper text the comment is attached to
    section_num: int | None  # 1..9 where derivable
    section_title: str


def read_review_comments(path: str | Path) -> list[ReviewComment]:
    doc = Document(str(path))
    # comment bodies by id
    bodies: dict[str, tuple[str, str]] = {
        str(c.comment_id): (c.author or "", c.text or "") for c in doc.comments
    }

    anchored: dict[str, str] = {}
    section_of: dict[str, tuple[int | None, str]] = {}
    open_ranges: dict[str, None] = {}
    state = {"title": "", "num": None}

    def walk(el) -> None:
        tag = _local(el.tag)
        if tag == "p":
            style = el.find(f"{_W}pPr/{_W}pStyle")
            if style is not None and style.get(f"{_W}val") == "Heading1":
                title = _text_of(el)
                state["title"] = title
                m = re.match(r"\s*(\d+)", title)
                state["num"] = int(m.group(1)) if m else None
            for child in el:
                walk(child)
            return
        if tag == "commentRangeStart":
            cid = el.get(f"{_W}id")
            open_ranges[cid] = None
            anchored.setdefault(cid, "")
            section_of[cid] = (state["num"], state["title"])
        elif tag == "commentRangeEnd":
            open_ranges.pop(el.get(f"{_W}id"), None)
        elif tag == "t":
            for cid in open_ranges:
                anchored[cid] = anchored.get(cid, "") + (el.text or "")
        else:
            for child in el:
                walk(child)

    walk(doc.element.body)

    out: list[ReviewComment] = []
    for cid, (author, text) in bodies.items():
        num, title = section_of.get(cid, (None, ""))
        out.append(
            ReviewComment(
                comment_id=cid,
                author=author,
                text=text,
                anchored_text=anchored.get(cid, "").strip(),
                section_num=num,
                section_title=title.strip(),
            )
        )
    out.sort(key=lambda c: (c.section_num or 99, c.comment_id))
    return out
