from creditpaper.db import cursor

with cursor() as cur:
    cur.execute("SELECT id, name, source, is_default, paper_types FROM templates ORDER BY created_at")
    rows = cur.fetchall()
    print(f"{len(rows)} templates:")
    for r in rows:
        print(f"  {r}")

    print()
    cur.execute("SELECT id, name FROM brand_profile LIMIT 3")
    brands = cur.fetchall()
    print(f"Brand profiles: {brands}")
