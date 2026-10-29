"""Read-only: check if lost migration also did table DDL beyond the 7 functions."""

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

for table in ("invitations", "tenants", "user_roles"):
    cur.execute(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema='public' AND table_name=%s "
        "ORDER BY ordinal_position",
        (table,),
    )
    rows = cur.fetchall()
    print(f"\n=== {table} ({len(rows)} cols) ===")
    for r in rows:
        print(f"  {r[0]:24s} {r[1]:24s} null={r[2]} default={r[3]}")

cur.execute(
    "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
    "AND tablename IN ('invitations','tenants','user_roles') ORDER BY indexname"
)
print("\n=== indexes ===")
print([r[0] for r in cur.fetchall()])

# Any triggers on these tables?
cur.execute(
    "SELECT c.relname, t.tgname FROM pg_trigger t "
    "JOIN pg_class c ON c.oid=t.tgrelid "
    "JOIN pg_namespace n ON n.oid=c.relnamespace "
    "WHERE n.nspname='public' AND c.relname IN ('invitations','tenants','user_roles') "
    "AND NOT t.tgisinternal"
)
print("\n=== triggers ===")
print([(r[0], r[1]) for r in cur.fetchall()])

conn.close()
