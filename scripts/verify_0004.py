from creditpaper.db import cursor

with cursor() as cur:
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name"
    )
    tables = [r[0] for r in cur.fetchall()]
    print(f"Tables ({len(tables)}):")
    for t in tables:
        print(f"  - {t}")

    print()
    cur.execute("SELECT id, label, sort_order FROM paper_types ORDER BY sort_order")
    print("paper_types:")
    for r in cur.fetchall():
        print(f"  {r[0]}: {r[1]} (order={r[2]})")
