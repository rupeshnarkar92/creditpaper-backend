"""One-off: revert auth_audit_events.tenant_id hand-drift (uuid -> varchar(36)).

The live remote table diverged from committed migration 0025_auth_audit_events
(varchar(36)). 0 rows, no policies/triggers/views/functions depend on it.
Reverting lets 0037_auth_signup_sp's record_auth_event apply unchanged.
"""

import os

from dotenv import load_dotenv

try:
    from creditpaper.env_file import env_file_path

    load_dotenv(env_file_path())
except ImportError:
    pass

import psycopg

url = os.getenv("DATABASE_URL", "")
url = url.replace("postgresql+asyncpg://", "postgresql://")
if "supabase" in url.lower() and "sslmode" not in url.lower():
    url += ("&" if "?" in url else "?") + "sslmode=require"

conn = psycopg.connect(url)
conn.autocommit = False
cur = conn.cursor()

cur.execute("SELECT count(*) FROM auth_audit_events WHERE tenant_id IS NOT NULL")
n = cur.fetchone()[0]
if n != 0:
    raise SystemExit(f"ABORT: {n} rows have non-null tenant_id; manual review")

cur.execute(
    "ALTER TABLE auth_audit_events ALTER COLUMN tenant_id TYPE character varying(36) USING tenant_id::text"
)
conn.commit()

cur.execute(
    "SELECT data_type FROM information_schema.columns "
    "WHERE table_schema='public' AND table_name='auth_audit_events' "
    "AND column_name='tenant_id'"
)
print("tenant_id now:", cur.fetchone()[0])
conn.close()
