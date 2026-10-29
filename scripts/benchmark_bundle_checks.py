"""Read-only comparison of cold and revalidated bundle findings for an existing deal."""
import argparse
import json
import os
import time

import psycopg
from dotenv import load_dotenv

from creditpaper.env_file import env_file_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deal", required=True)
    parser.add_argument("--restricted-role", action="store_true",
                        help="Also SET LOCAL ROLE creditpaper_runtime; requires role membership")
    args = parser.parse_args()
    load_dotenv(env_file_path())
    from creditpaper.deals.services import slot_board as board

    dsn = (os.environ.get("MIGRATION_DATABASE_URL") or os.environ["DATABASE_URL"])
    dsn = dsn.replace("+psycopg", "").replace("+asyncpg", "")
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        tenant = str(conn.execute("SELECT tenant_id FROM deals WHERE id=%s", (args.deal,)).fetchone()[0])
        # Existing assignments only; avoid the board's lazy-create path.
        conn.execute("SELECT set_config('app.tenant_id',%s,true)", (tenant,))
        versions = [str(r[0]) for r in conn.execute(
            "SELECT DISTINCT version_id FROM public.document_slot_file_rows(%s,%s) "
            "WHERE role='assigned' AND version_id IS NOT NULL", (tenant, args.deal))]
        if args.restricted_role:
            conn.execute("SET LOCAL ROLE creditpaper_runtime")
        visible = conn.execute(
            "SELECT version_id,fingerprint FROM public.slot_bundle_inputs(%s,%s::uuid[],%s::jsonb)",
            (tenant, versions, "{}"),
        ).fetchall()
        assert visible, "Connection role cannot see own extractions"
        known = json.dumps({str(v): fp for v, fp in visible})
        assert conn.execute(
            "SELECT count(*) FROM public.slot_bundle_inputs(%s,%s::uuid[],%s::jsonb) "
            "WHERE page_texts IS NOT NULL", (tenant, versions, known),
        ).fetchone()[0] == 0, "Unchanged text was retransferred"
        conn.execute("SELECT set_config('app.tenant_id',%s,true)",
                     ("00000000-0000-0000-0000-000000000001",))
        assert conn.execute(
            "SELECT count(*) FROM public.slot_bundle_inputs(%s,%s::uuid[],%s::jsonb)",
            (tenant, versions, known),
        ).fetchone()[0] == 0, "Mismatched tenant context exposed data"
        conn.rollback()
    board._bundle_cache.clear()
    start = time.perf_counter()
    cold = board._bundle_checks(tenant, versions)
    cold_s = time.perf_counter() - start
    start = time.perf_counter()
    warm = board._bundle_checks(tenant, versions)
    warm_s = time.perf_counter() - start
    assert warm == cold, "Revalidated results differ"
    # Full-page baseline runs the unchanged detection algorithm.
    assert cold == board._checks_from_pages(board._pages_by_version(tenant, versions))
    print({"versions": len(versions), "findings": len(cold), "cold_s": round(cold_s, 3),
           "warm_s": round(warm_s, 3), "parity": True,
           "tenant_context_checks": True, "restricted_role_checks": args.restricted_role})


if __name__ == "__main__":
    main()
