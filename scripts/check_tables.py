from creditpaper.db import cursor

with cursor() as cur:
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name"
    )
    rows = cur.fetchall()
    print(f"{len(rows)} tables in Supabase:")
    for (t,) in rows:
        print(f"  - {t}")
