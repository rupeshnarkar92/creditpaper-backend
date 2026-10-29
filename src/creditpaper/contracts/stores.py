"""Storage seams, as interfaces rather than as imports of a file-backed module.

Generated prose and captured signals are file-backed today, under `output/`. The
swap target is Supabase, and the platform team owns that implementation. These
Protocols are what the swap has to satisfy, written down so the interface is
agreed before one side builds against the other's current shape.

Deliberately narrow. Every method here is one the API already calls, so this is a
description of the boundary that exists rather than a speculative abstraction —
`harness/prose_store.py` and `harness/signal_store.py` satisfy them today without
modification, which is the test of whether the line was drawn in the right place.

All reads require explicit tenant identity. HTTP adapters derive it from the authenticated user.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from creditpaper.contracts.signal import Signal

ProseRecord = dict[str, Any]
"""One section's stored prose. Deliberately loose: the on-disk shape predates this
contract and the platform reads it through `contracts.prose.ProseBlock`, which is
where the real schema lives. Naming it here at least makes the looseness explicit
rather than an unparameterised `dict` that mypy waves through."""


@runtime_checkable
class ProseStore(Protocol):
    """Reads generated section prose for a deal."""

    def list_deal_ids(self, tenant_id: str) -> list[str]:
        """Every deal with cached prose."""
        ...

    def load_prose(self, deal_id: str, *, tenant_id: str) -> dict[str, ProseRecord] | None:
        """The cached prose blocks for a deal, keyed by section number as a string.

        Implementations MUST validate `deal_id` before using it to address
        storage. The file-backed one joins it into a path, and an unvalidated id
        from an HTTP path parameter is arbitrary file read and append.
        """
        ...


@runtime_checkable
class SignalStore(Protocol):
    """Append-only capture of human comments and edits.

    Append-only is a property the interface guarantees, not an implementation
    detail: the original AI draft is never overwritten, only superseded, and the
    learning substrate depends on the full history being intact.
    """

    def append_signal(self, signal: Signal) -> Signal:
        """Record one signal and return it as stored."""
        ...

    def read_signals(self, deal_id: str, section: int | None = None, *, tenant_id: str) -> list[Signal]:
        """Every signal for a deal, optionally narrowed to one section."""
        ...

    def latest_edit_text(self, deal_id: str, section: int, *, tenant_id: str) -> str | None:
        """The current text for a section, or None if it has never been edited.

        "Latest" means last appended, NOT newest timestamp: two edits saved inside
        one clock tick carry the same timestamp, and selecting by maximum returned
        the older one — silently discarding the analyst's most recent work. The
        store is append-only, so order is the authority and needs no clock.
        """
        ...
