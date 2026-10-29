"""Group the same document's versions, and say which one is operative.

Exact duplicates are cheap to find and nearly pointless (2.3% of fixture pages).
The expensive problem is the 12.5% of documents that sit in near-identical
families: a draft, a reissue, and the executed copy, all sharing 95% of their
text. D6's cross-document checks reconcile figures across documents, so an
unresolved family means a superseded draft silently contradicting the signed
instrument, and a check that fires on a term nobody agreed to.

The draft/executed call has four outcomes, not two. The fixture corpus held 27
documents carrying BOTH draft and execution markers - a DRAFT watermark left on
a signed copy, or an execution block with placeholders still in it. Those are
not a coin flip; CONFLICTING sends them to a person, which is what D5 requires
when the operative instrument is ambiguous.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel

SIMILARITY_THRESHOLD = 0.70
_SHINGLE = 5
_MAX_CHARS = 20000

_EXECUTED_RE = re.compile(
    r"\b(executed as an agreement|executed as a deed|signed by the authorised representative"
    r"|signature envelope|docusign envelope|adobe sign|electronically signed|witnessed by)\b",
    re.I,
)
# Two patterns, not one. The PHRASES are case-insensitive: "Draft For Discussion"
# is the same signal as "draft for discussion". The bare token is not, because
# "the draft was revised" is ordinary prose in an executed document, while a
# DRAFT watermark is shouted in capitals by every tool that applies one.
_DRAFT_PHRASE_RE = re.compile(
    r"\b(draft for discussion|draft only|draft copy|not for execution|subject to contract"
    r"|preliminary draft)\b",
    re.I,
)
_DRAFT_TOKEN_RE = re.compile(r"\bDRAFT\b")
_PLACEHOLDER_RE = re.compile(r"(\[DATE\]|\[INSERT[^\]]*\]|_{6,}|x{6,}|\bTBC\b|\bTBA\b)", re.I)


class VersionStatus(str, Enum):
    EXECUTED = "executed"
    DRAFT = "draft"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class FamilyMember(BaseModel):
    content_hash: str
    status: VersionStatus
    superseded_by: str | None = None


def version_status(text: str) -> VersionStatus:
    head = (text or "")[:_MAX_CHARS]
    executed = bool(_EXECUTED_RE.search(head))
    drafted = (
        bool(_DRAFT_PHRASE_RE.search(head))
        or bool(_DRAFT_TOKEN_RE.search(head))
        or bool(_PLACEHOLDER_RE.search(head))
    )
    if executed and drafted:
        return VersionStatus.CONFLICTING
    if executed:
        return VersionStatus.EXECUTED
    if drafted:
        return VersionStatus.DRAFT
    return VersionStatus.UNKNOWN


def _shingles(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "")[:_MAX_CHARS].lower())
    if len(words) < _SHINGLE:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + _SHINGLE]) for i in range(len(words) - _SHINGLE + 1)}


def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_RANK = {
    VersionStatus.EXECUTED: 0,
    VersionStatus.CONFLICTING: 1,
    VersionStatus.UNKNOWN: 2,
    VersionStatus.DRAFT: 3,
}


def cluster_families(docs: list[tuple[str, str]]) -> list[list[FamilyMember]]:
    prepared = [(h, text, _shingles(text)) for h, text in docs]
    parent = {h: h for h, _, _ in prepared}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, (h_i, _, sh_i) in enumerate(prepared):
        for h_j, _, sh_j in prepared[i + 1 :]:
            if _similarity(sh_i, sh_j) >= SIMILARITY_THRESHOLD:
                parent[find(h_j)] = find(h_i)

    grouped: dict[str, list[FamilyMember]] = {}
    for h, text, _ in prepared:
        grouped.setdefault(find(h), []).append(FamilyMember(content_hash=h, status=version_status(text)))

    families: list[list[FamilyMember]] = []
    for members in grouped.values():
        members.sort(key=lambda m: _RANK[m.status])
        operative = members[0]
        if len(members) > 1 and operative.status is VersionStatus.EXECUTED:
            for member in members[1:]:
                member.superseded_by = operative.content_hash
        families.append(members)
    return families
