import sys

sys.path.insert(0, "src")
from creditpaper.db import get_pool

# Delete the brand_profile and onboarding_state rows for tenant 1
with get_pool().connection() as conn:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM brand_profile WHERE tenant_id::text = %s", ("1",))
        cur.execute("DELETE FROM onboarding_state WHERE tenant_id::text = %s", ("1",))
        print("deleted")
