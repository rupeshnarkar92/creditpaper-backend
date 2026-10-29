"""Replay matching on a deal snapshot; rollback writes and suppress downstream jobs."""

import argparse
import copy
import json
import time
from contextlib import contextmanager
from unittest.mock import patch

from creditpaper import db
from creditpaper.db import close_pool, read_cursor
from creditpaper.deals.services import slot_board
from creditpaper.deals.store import slot_store


class RollbackProbe(Exception):
    pass


@contextmanager
def rollback_database():
    """Roll back every checkout, including read helpers that lazily create rows.

    This standalone probe is single-threaded. Nested application transactions
    become savepoints; the outer transaction never commits. Timings therefore
    include the extra transaction boundary and are not production latency.
    """
    original_get_pool = db.get_pool

    class RollbackPool:
        @contextmanager
        def connection(self, *args, **kwargs):
            with original_get_pool().connection(*args, **kwargs) as conn:
                with conn.transaction(force_rollback=True):
                    yield conn

    with patch.object(db, "get_pool", return_value=RollbackPool()):
        yield


def main():
    with rollback_database():
        run_probe()


def run_probe():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deal", required=True)
    parser.add_argument("--background", action="store_true", help="Skip the unused worker board response")
    args = parser.parse_args()
    with read_cursor() as cur:
        cur.execute("SELECT tenant_id FROM deals WHERE id=%s", (args.deal,))
        row = cur.fetchone()
    if not row:
        raise ValueError("Deal not found")
    tenant = str(row[0])
    timings = {}
    counts = {}
    timing_depth = 0

    def timed(name, fn):
        def wrapped(*a, **kw):
            nonlocal timing_depth
            if timing_depth:
                return fn(*a, **kw)
            timing_depth += 1
            start = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                timings[name] = timings.get(name, 0) + time.perf_counter() - start
                timing_depth -= 1

        return wrapped

    original_load = slot_board._load
    original_write = slot_store.write_changes
    original_cursor = slot_store.tenant_cursor
    original_board = slot_board.get_board

    def board_response(*a, **kw):
        with patch.object(slot_board, "_load", original_load):
            return original_board(*a, **kw)

    def fresh_snapshot(*a):
        ctx = copy.deepcopy(original_load(*a))
        counts["documents"] = len(ctx.documents)
        ctx.files = []
        for slot in ctx.rows.values():
            slot.update(status="empty", confidence=None, match_reason=None, error=None)
        return ctx

    @contextmanager
    def rollback_cursor(*a):
        with original_cursor(*a) as cur:
            yield cur
            raise RollbackProbe()

    def rollback_write(**kw):
        counts["file_mutations"] = len(kw.get("add_files") or [])
        counts["slot_updates"] = len(kw.get("slot_updates") or [])
        try:
            with patch.object(slot_store, "tenant_cursor", rollback_cursor):
                original_write(**kw)
        except RollbackProbe:
            pass

    from contextlib import ExitStack

    start = time.perf_counter()
    with ExitStack() as stack:
        for obj, name, fn, label in [
            (slot_board, "_load", fresh_snapshot, "board_load"),
            (slot_store, "match_pages", slot_store.match_pages, "pages_read"),
            (slot_board, "_labelled_corpus", slot_board._labelled_corpus, "labelled_read"),
            (slot_store, "match_inputs", slot_store.match_inputs, "supersession_read"),
            (slot_store, "write_changes", rollback_write, "writes_rolled_back"),
            (slot_board, "get_board", board_response, "board_response"),
        ]:
            stack.enter_context(patch.object(obj, name, timed(label, fn)))
        stack.enter_context(patch.object(slot_board, "_start_speculation"))
        slot_board.propose(tenant, args.deal, None, return_board=not args.background)
    total = time.perf_counter() - start
    timings["other_matching_logic"] = total - sum(timings.values())
    print(
        json.dumps(
            {
                "counts": counts,
                "seconds": timings,
                "total_seconds": total,
                "writes": "rolled back",
                "downstream_jobs": "suppressed",
                "timing_note": "Includes rollback-only transaction overhead",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        close_pool()
