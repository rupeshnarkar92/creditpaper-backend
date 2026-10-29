"""Unread pages are stated, never inferred from silence.

A document whose pages failed to parse must not look complete. Docling returns
zero items for a scanned page when OCR is off — no error, no content — and an
extractor reading that would report a field as absent rather than unread. The
difference matters to a credit decision: "the ITS does not state a facility
limit" and "page 4 was never read" are not the same finding.

A field whose source page was not read loses its value AND its provenance: a
MISSING fact must not carry an anchor to a page nothing was read from.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel

from creditpaper.model.core import Fact, FactStatus

NOT_READ_NOTE = "missing: not read"


class ReadCoverage(Protocol):
    """Which pages were not read, structurally.

    Defined here rather than in `commit.py` because `mark_unread_fields` takes
    one and `gaps` cannot import `commit` — `commit` imports `gaps`.

    PROPERTIES, not attributes: `Coverage` computes both, and a protocol that
    declares `has_gaps: bool` is asking for a mutable attribute, which a
    read-only property does not satisfy. Declared this way the concrete class
    and the protocol agree, which is what lets the two ingestion lines pass
    coverage to each other's functions.
    """

    @property
    def has_gaps(self) -> bool: ...

    @property
    def unread(self) -> Sequence[int]: ...


class Coverage(BaseModel):
    requested: tuple[int, ...]
    read: tuple[int, ...]
    failed: tuple[int, ...] = ()

    @property
    def unread(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.requested) - set(self.read)))

    @property
    def has_gaps(self) -> bool:
        return bool(self.unread)


def coverage_from(requested: Sequence[int], read: Sequence[int], failed: Sequence[int] = ()) -> Coverage:
    return Coverage(
        requested=tuple(sorted(set(requested))),
        read=tuple(sorted(set(read))),
        failed=tuple(sorted(set(failed))),
    )


def mark_unread_fields(fields: dict[str, Fact[Any]], coverage: ReadCoverage) -> dict[str, Fact[Any]]:
    if not coverage.has_gaps:
        return dict(fields)
    unread = set(coverage.unread)
    out: dict[str, Fact[Any]] = {}
    for path, fact in fields.items():
        page = fact.provenance.page if fact.provenance else None
        if page is not None and page in unread:
            out[path] = fact.model_copy(
                update={
                    "value": None,
                    "provenance": None,
                    "confidence": None,
                    "status": FactStatus.MISSING,
                }
            )
        else:
            out[path] = fact
    return out
