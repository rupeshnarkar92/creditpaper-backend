"""Verify archive routines under the runtime role; all fixture writes roll back."""

import argparse
import os
from uuid import uuid4

import psycopg
from dotenv import load_dotenv

from creditpaper.env_file import env_file_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-role", action="store_true")
    args = parser.parse_args()
    load_dotenv(env_file_path())
    dsn = os.environ.get("MIGRATION_DATABASE_URL") or os.environ["DATABASE_URL"]
    for driver in ("+asyncpg", "+psycopg", "+psycopg2"):
        dsn = dsn.replace(driver, "")
    with psycopg.connect(
        dsn, connect_timeout=10, options="-c statement_timeout=15000 -c lock_timeout=3000"
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            print("Database revisions:", ", ".join(row[0] for row in cur.fetchall()))
            cur.execute("SELECT tenant_id,id FROM public.deals LIMIT 1")
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("No visible deal available for rollback verification")
            tenant, deal = row
            batch = uuid4()
            if not args.current_role:
                cur.execute("SET LOCAL ROLE creditpaper_runtime")
            cur.execute("SELECT set_config('app.tenant_id',%s,true)", (str(tenant),))
            cur.execute("SELECT public.deal_upload_batch_write(%s,%s,%s,2,0,false)", (tenant, deal, batch))
            cur.execute(
                "SELECT public.accept_document_archive(%s,%s,%s,%s,%s,2)",
                (tenant, deal, batch, uuid4(), "rollback-only.zip.enc"),
            )
            assert cur.fetchone()[0]
            cur.execute(
                "SELECT public.document_archive_checkpoint(%s,%s,%s,%s::jsonb,false)",
                (tenant, deal, batch, '{"test.txt":{"status":"uploaded"}}'),
            )
            cur.execute("SELECT public.document_archive_progress(%s,%s)", (tenant, deal))
            assert any(b["id"] == str(batch) and b["processed"] == 1 for b in cur.fetchone()[0])
            cur.execute("SELECT public.deal_ingestion_status(%s,%s)", (tenant, deal))
            assert cur.fetchone()[0]["locked"]
            if not args.current_role:
                cur.execute("SELECT set_config('app.tenant_id',%s,true)", (str(uuid4()),))
                cur.execute("SELECT public.document_archive(%s,%s,%s)", (tenant, deal, batch))
                assert cur.fetchone()[0] is None, "Tenant isolation failed"
        conn.rollback()
    print("Receipt, checkpoint, progress and lock checks passed; writes rolled back.")
    print("Cross-tenant runtime check: " + ("NOT RUN" if args.current_role else "passed"))


if __name__ == "__main__":
    main()
