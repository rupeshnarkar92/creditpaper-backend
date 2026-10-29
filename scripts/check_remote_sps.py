"""Check stored procedures exist on remote Supabase DB."""

import os

from dotenv import load_dotenv

try:
    from creditpaper.env_file import env_file_path

    load_dotenv(env_file_path())
except Exception:
    pass
import psycopg

url = os.getenv("DATABASE_URL", "")
url = url.replace("postgresql+asyncpg://", "postgresql://")
if "supabase" in url.lower() and "sslmode" not in url.lower():
    url += ("&" if "?" in url else "?") + "sslmode=require"
conn = psycopg.connect(url)
cur = conn.cursor()
cur.execute(
    "SELECT proname FROM pg_proc "
    "WHERE proname IN ('get_workspace_page', 'get_workspace_payload', "
    "'get_slot_board', 'get_fact_sheet_data', 'get_tenant_activity') "
    "ORDER BY proname"
)
rows = cur.fetchall()
print("Stored procedures on Supabase:", [r[0] for r in rows])
cur.execute("SELECT version_num FROM alembic_version")
ver = cur.fetchall()
print("Alembic version on Supabase:", [r[0] for r in ver])
conn.close()
