from __future__ import annotations

import uuid

from creditpaper.contracts import Signal, SignalKind
from creditpaper.db import tenant_cursor, tenant_uuid


def _row_to_signal(row: tuple, tenant_id: str) -> Signal:
    (id_, deal_id, section, kind, author, comment_text, anchored, before, after, created) = row
    return Signal(
        id=str(id_),
        tenant_id=str(tenant_id),
        deal_id=deal_id,
        section=section,
        kind=SignalKind(kind),
        author=author,
        comment_text=comment_text,
        anchored_text=anchored,
        before_text=before,
        after_text=after,
        created_at=created,
    )


def append_signal(signal: Signal) -> Signal:
    tid = tenant_uuid(signal.tenant_id)
    sig_id = signal.id or uuid.uuid4()
    with tenant_cursor(str(tid)) as cur:
        cur.execute(
            "SELECT * FROM public.append_signal(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                sig_id,
                tid,
                signal.deal_id,
                signal.section,
                signal.kind.value,
                signal.author,
                signal.comment_text,
                signal.anchored_text,
                signal.before_text,
                signal.after_text,
            ),
        )
        row = cur.fetchone()
    return _row_to_signal(row, str(tid))


def read_signals(deal_id: str, section: int | None = None, *, tenant_id: str) -> list[Signal]:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT * FROM public.list_signals(%s,%s,%s)", (tid, deal_id, section))
        rows = cur.fetchall()
    return [_row_to_signal(r, tenant_id) for r in rows]


def latest_edit_text(deal_id: str, section: int, *, tenant_id: str) -> str | None:
    tid = tenant_uuid(tenant_id)
    with tenant_cursor(str(tid)) as cur:
        cur.execute("SELECT public.latest_signal_edit(%s,%s,%s)", (tid, deal_id, section))
        row = cur.fetchone()
    return row[0] if row else None
