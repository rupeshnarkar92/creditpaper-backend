from creditpaper.db import cursor

with cursor() as cur:
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'brand_profile'")
    print("brand_profile columns:")
    for (c,) in cur.fetchall():
        print(f"  {c}")
