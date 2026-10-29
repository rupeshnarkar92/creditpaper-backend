from creditpaper.db import cursor

with cursor() as cur:
    cur.execute("SELECT tenant_id, profile FROM brand_profile")
    rows = cur.fetchall()
    print(f"brand_profile rows: {len(rows)}")
    for r in rows:
        print(f"  tenant: {r[0]}, name in profile: {r[1].get('brand', {}).get('name') if r[1] else 'N/A'}")

    print()
    cur.execute("SELECT tenant_id, state FROM onboarding_state")
    rows = cur.fetchall()
    print(f"onboarding_state rows: {len(rows)}")
    for r in rows:
        completed = r[1].get("completed_steps", []) if r[1] else []
        print(f"  tenant: {r[0]}, completed: {completed}")

    print()
    cur.execute("SELECT tenant_id FROM voice_profile")
    rows = cur.fetchall()
    print(f"voice_profile rows: {len(rows)}")
    for r in rows:
        print(f"  tenant: {r[0]}")
