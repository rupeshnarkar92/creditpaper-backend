"""Read-only: dump orphan function defs + tables vs migration expectations."""

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

orphans = [
    "get_tenant_members",
    "update_tenant_member",
    "create_tenant_invitation",
    "cancel_tenant_invitation",
    "get_tenant_invitations",
    "resend_tenant_invitation",
    "resolve_tenant_domain_id",
]

print("=== alembic_version ===")
cur.execute("SELECT version_num FROM alembic_version")
print([r[0] for r in cur.fetchall()])

print("\n=== orphan function signatures + acls ===")
for name in orphans:
    cur.execute(
        "SELECT p.oid::regprocedure::text, pg_get_functiondef(p.oid), "
        "array_to_string(p.proacl, ' ') "
        "FROM pg_proc p WHERE p.pronamespace='public'::regnamespace AND p.proname=%s",
        (name,),
    )
    row = cur.fetchone()
    if not row:
        print(f"{name}: MISSING")
        continue
    print(f"{name}: sig={row[0]} acl={row[2]}")

print("\n=== tables referenced by invitations/members ===")
cur.execute(
    "SELECT tablename FROM pg_tables WHERE schemaname='public' "
    "AND (tablename LIKE '%%invitation%%' OR tablename LIKE '%%member%%' "
    "OR tablename LIKE '%%tenant%%' OR tablename='user_roles') ORDER BY tablename"
)
print([r[0] for r in cur.fetchall()])

print("\n=== all public functions (count by prefix) ===")
cur.execute(
    "SELECT proname FROM pg_proc WHERE pronamespace='public'::regnamespace "
    "AND proname NOT LIKE 'pg_%' ORDER BY proname"
)
print([r[0] for r in cur.fetchall()])

print("\n=== auth-context functions needed by sign-in ===")
needed = [
    "get_signup_email_status",
    "get_supabase_auth_context",
    "get_user_membership",
    "resolve_application_user",
    "provision_tenant_for_user",
    "record_password_history",
    "get_password_history_hashes",
    "get_password_history_count",
    "record_auth_event",
    "get_home_dashboard",
    "get_authenticated_application_context",
]
cur.execute(
    "SELECT proname FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname = ANY(%s)",
    (needed,),
)
present = {r[0] for r in cur.fetchall()}
for n in needed:
    print(f"  {'OK ' if n in present else 'GAP'} {n}")

conn.close()
