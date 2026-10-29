from creditpaper.db import cursor

with cursor() as cur:
    cur.execute(
        "SELECT id, email, is_super_admin, LEFT(password_hash, 30) FROM users WHERE email = 'superadmin@creditpaper.com'"
    )
    rows = cur.fetchall()
    for r in rows:
        print(r)
