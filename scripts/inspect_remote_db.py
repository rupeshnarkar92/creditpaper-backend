"""Inspect remote Supabase: alembic version + public functions vs expected migrations."""

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

cur.execute(
    "SELECT p.proname, l.lanname, p.provolatile, p.prosecdef "
    "FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang "
    "WHERE p.pronamespace = 'public'::regnamespace "
    "AND p.proname NOT LIKE 'pg_%' "
    "ORDER BY p.proname"
)
rows = cur.fetchall()
print(f"\npublic functions ({len(rows)}):")
for name, _lang, vol, secdef in rows:
    tags = []
    if secdef:
        tags.append("SECDEF")
    tags.append(vol)
    print(f"  {name} [{','.join(tags)}]")

# Functions that 0037_auth_signup_sp should have created
expected_0037 = [
    "get_signup_email_status",
]
cur.execute(
    "SELECT proname FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname = ANY(%s)",
    (expected_0037,),
)
found = [r[0] for r in cur.fetchall()]
print("\n0037_auth_signup_sp functions present:", found or "NONE")

# 0038 home dashboard
cur.execute(
    "SELECT proname FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname LIKE '%%home%%'"
)
print("0038-ish home functions:", [r[0] for r in cur.fetchall()] or "NONE")

# 0039
cur.execute(
    "SELECT proname FROM pg_proc "
    "WHERE pronamespace='public'::regnamespace "
    "AND proname = 'get_authenticated_application_context'"
)
print("0039 function present:", [r[0] for r in cur.fetchall()] or "NONE")

conn.close()
