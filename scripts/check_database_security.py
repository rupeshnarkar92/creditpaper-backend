"""Read-only deployment checks. Output contains control names, never credentials."""

from __future__ import annotations

import json

from creditpaper.db import close_pool, cursor


def checks(cur) -> dict[str, bool]:
    cur.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
    superuser, bypass = cur.fetchone()
    result = {"runtime_not_superuser": not superuser, "runtime_cannot_bypass_rls": not bypass}
    cur.execute("""SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,
        c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r'
          AND (c.relname LIKE 'ledger_%' OR c.relname='processing_jobs')""")
    rows = cur.fetchall()
    result["canonical_schema_present"] = len(rows) >= 15
    result["rls_enabled_and_forced"] = bool(rows) and all(r[1] and r[2] for r in rows)
    result["runtime_not_table_owner"] = bool(rows) and not any(r[3] for r in rows)
    for name, _, _, _ in rows:
        if name.startswith("ledger_"):
            cur.execute(
                "SELECT has_table_privilege(current_user,%s,'UPDATE,DELETE,TRUNCATE,TRIGGER')", (name,)
            )
            result[f"{name}_no_destructive_grant"] = not cur.fetchone()[0]
    cur.execute("""SELECT count(*) FROM information_schema.role_table_grants
        WHERE table_schema='public' AND grantee IN ('PUBLIC','anon','authenticated')
          AND (table_name LIKE 'ledger_%' OR table_name IN ('deals','deal_document_versions',
               'deal_document_extractions','deal_model_snapshots','processing_jobs','fact_review_queue'))""")
    result["no_direct_browser_data_grants"] = cur.fetchone()[0] == 0
    cur.execute("SELECT has_schema_privilege(current_user,'public','CREATE')")
    result["runtime_cannot_create_schema_objects"] = not cur.fetchone()[0]
    return result


def main():
    try:
        with cursor() as cur:
            result = checks(cur)
        print(json.dumps(result, sort_keys=True, indent=2))
        raise SystemExit(0 if all(result.values()) else 1)
    finally:
        close_pool()


if __name__ == "__main__":
    main()
