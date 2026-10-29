"""Read-only: live auth_audit_events schema, 0037 partial state, alembic current."""

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
cur = conn.cursor()

cur.execute("SELECT version_num FROM alembic_version")
print("alembic_version:", [r[0] for r in cur.fetchall()])

for table in ("auth_audit_events", "deal_audit_events"):
    cur.execute(
        "SELECT column_name, data_type, udt_name, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
        (table,),
    )
    rows = cur.fetchall()
    print(f"\n=== {table} ({len(rows)} cols) ===")
    for r in rows:
        print(f"  {r[0]:20s} {r[1]:16s} udt={r[2]:10s} null={r[3]}")

cur.execute(
    "SELECT proname FROM pg_proc WHERE pronamespace='public'::regnamespace "
    "AND proname IN ('get_signup_email_status','get_supabase_auth_context',"
    "'get_user_membership','resolve_application_user','provision_tenant_for_user',"
    "'record_password_history','get_password_history_hashes','get_password_history_count',"
    "'record_auth_event','get_home_dashboard','get_authenticated_application_context') "
    "ORDER BY proname"
)
found = [r[0] for r in cur.fetchall()]
print("\n0037/0038/0039 functions present:", found or "NONE")

cur.execute("SELECT count(*) FROM auth_audit_events")
print("auth_audit_events row count:", cur.fetchone()[0])

conn.close()
