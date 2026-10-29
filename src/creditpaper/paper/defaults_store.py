"""Reads of the `platform_defaults` table (platform data, not tenant data).

One current row per kind (`template`, `paper_theme`, `voice`), guaranteed by the seed
migration `0018_template_and_paper_theme`. A missing current row is a deployment
fault, not a runtime condition to paper over: it raises, and the app's startup check
(`verify_platform_defaults`) fails loudly on it.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from creditpaper.db import cursor

KINDS = ("template", "paper_theme", "voice")

_CACHE_TTL_S = 60.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


class PlatformDefaultMissing(RuntimeError):
    """No current `platform_defaults` row for a kind."""


def _fetch_current(kind: str) -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM public.get_current_platform_default(%s)", (kind,))
        row = cur.fetchone()
    if not row:
        return None
    return {
        "id": str(row[0]),
        "kind": row[1],
        "version": int(row[2]),
        "payload": row[3] or {},
        "created_at": row[4].isoformat() if row[4] else None,
    }


def load_current(kind: str) -> dict[str, Any]:
    """The current record for `kind` as `{id, kind, version, payload, created_at}`.

    Cached per process for a minute: these rows change only by migration or an
    operator publishing a new version.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown platform default kind: {kind!r}")
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(kind)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    record = _fetch_current(kind)
    if record is None:
        raise PlatformDefaultMissing(
            f"no current platform_defaults row for kind '{kind}' — run `alembic upgrade head`"
        )
    with _cache_lock:
        _cache[kind] = (now, record)
    return record


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def verify_platform_defaults() -> None:
    """Startup check: every kind has a current row. Raises PlatformDefaultMissing."""
    for kind in KINDS:
        load_current(kind)
