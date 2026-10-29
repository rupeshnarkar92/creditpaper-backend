"""Merge rn_001_lookup_tables with 0043_ingestion_data.

Both branches descend from 0042_merge_tenant_gateway: the ingestion branch
adds 0043_ingestion_data, while the RBAC/lookups branch adds
rn_001_lookup_tables. This revision rejoins them into a single head.
"""

revision = "eeba3e2314cf"
down_revision = ("0043_ingestion_data", "rn_001_lookup_tables")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
