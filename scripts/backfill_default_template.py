"""Backfill default template for tenants who completed onboarding
but have no default template row.

This is a one-time migration script for existing tenants.
New tenants will get the default template automatically via
onboarding/route/state.py:post_complete().
"""

from creditpaper.db import cursor
from creditpaper.templates.store import template_store


def backfill_default(tenant_id: str) -> None:
    with cursor() as cur:
        cur.execute("SELECT profile FROM brand_profile WHERE tenant_id = %s", (tenant_id,))
        row = cur.fetchone()
        brand = row[0] if row else None

        cur.execute("SELECT sections FROM section_orders WHERE tenant_id = %s", (tenant_id,))
        row = cur.fetchone()
        sections = row[0] if row else []

    if template_store.get_default(tenant_id) is not None:
        print(f"Tenant {tenant_id}: default template already exists, skipping")
        return

    name = (brand or {}).get("brand", {}).get("name") or "Default Template"
    template_store.create(
        tenant_id=tenant_id,
        name=name,
        description="Default template created during onboarding",
        paper_types=[],
        sections=sections or [],
        is_default=True,
        source="onboarding",
    )
    print(f"Tenant {tenant_id}: created default template '{name}'")


def main():
    with cursor() as cur:
        cur.execute("""
            SELECT DISTINCT tenant_id FROM (
                SELECT tenant_id FROM brand_profile
                UNION SELECT tenant_id FROM voice_profile
                UNION SELECT tenant_id FROM section_orders
            ) AS t
        """)
        tenant_ids = [r[0] for r in cur.fetchall()]

    print(f"Found {len(tenant_ids)} tenant(s) with onboarding data")
    for tid in tenant_ids:
        backfill_default(str(tid))


if __name__ == "__main__":
    main()
