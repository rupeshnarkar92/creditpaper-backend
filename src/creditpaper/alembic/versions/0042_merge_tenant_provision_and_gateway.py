"""Merge tenant provisioning fixes with the tenant AI Gateway schema.

Both branches descend from 0039_auth_app_context. Supabase databases may
already be stamped at 0041_provision_tenant_domains, while the gateway branch
adds the BYOK and integration control-plane schema.
"""

revision = "0042_merge_tenant_gateway"
down_revision = ("0041_provision_tenant_domains", "0041_ai_gateway_control_plane")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
