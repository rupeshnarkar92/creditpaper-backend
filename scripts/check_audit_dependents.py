"""Read-only: dependents on auth_audit_events before reverting tenant_id drift."""

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

cur.execute(
    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
    "WHERE oid = 'public.auth_audit_events'::regclass"
)
print("RLS enabled/forced:", cur.fetchone())

cur.execute("SELECT policyname, cmd, qual, with_check FROM pg_policies WHERE tablename = 'auth_audit_events'")
print("policies:", cur.fetchall() or "NONE")

cur.execute(
    "SELECT c.relname, t.tgname FROM pg_trigger t "
    "JOIN pg_class c ON c.oid = t.tgrelid "
    "WHERE c.relname = 'auth_audit_events' AND NOT t.tgisinternal"
)
print("triggers:", cur.fetchall() or "NONE")

cur.execute(
    "SELECT viewname FROM pg_views WHERE schemaname = 'public' AND definition LIKE '%%auth_audit_events%%'"
)
print("views:", cur.fetchall() or "NONE")

cur.execute(
    "SELECT p.proname FROM pg_proc p "
    "WHERE p.prosrc LIKE '%%auth_audit_events%%' "
    "AND p.pronamespace = 'public'::regnamespace"
)
print("functions referencing table:", cur.fetchall() or "NONE")

try:
    conn.rollback()
    cur.execute("CREATE TEMP TABLE _cast_test (j json)")
    cur.execute("INSERT INTO _cast_test VALUES (%s::jsonb)", ("{}",))
    conn.rollback()
    print("jsonb->json assignment: OK (rolled back)")
except Exception as exc:
    conn.rollback()
    print("jsonb->json assignment: FAILED:", exc)

conn.close()
