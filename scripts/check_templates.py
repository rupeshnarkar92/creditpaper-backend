from creditpaper.db import cursor

with cursor() as cur:
    # Get section_orders schema
    cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'section_orders'
        ORDER BY ordinal_position
    """)
    print("section_orders columns:")
    for r in cur.fetchall():
        print(f"  {r[0]}: {r[1]} (nullable={r[2]})")

    print()
    # Get sample data
    cur.execute("SELECT * FROM section_orders LIMIT 3")
    rows = cur.fetchall()
    print("section_orders sample:")
    for r in rows:
        print(f"  {r}")
