import psycopg2

conn = psycopg2.connect(host="localhost", port=5432, dbname="creditpaper", user="postgres", password="123456")
cur = conn.cursor()

cur.execute(
    "SELECT id, display_name, document_kind, archived_at FROM deal_documents WHERE deal_id = %s ORDER BY created_at",
    ("deal_dfe8c9b8a850",),
)
print("=== Documents for Riverbend Rise ===")
for r in cur.fetchall():
    print(f"  {r[1]}  kind={r[2]}  archived={r[3]}")

cur.execute(
    "SELECT section, status, prose IS NOT NULL as has_prose, length(prose) as prose_len FROM deal_paper_sections WHERE deal_id = %s ORDER BY section",
    ("deal_dfe8c9b8a850",),
)
print("\n=== Sections ===")
for r in cur.fetchall():
    print(f"  S{r[0]}  status={r[1]}  prose={'yes (' + str(r[3]) + ' chars)' if r[2] else 'no'}")

cur.execute("SELECT deal_id, name, status FROM deals WHERE id = %s", ("deal_dfe8c9b8a850",))
r = cur.fetchone()
print(f"\n=== Deal: {r[1]} ({r[2]}) ===")

conn.close()
