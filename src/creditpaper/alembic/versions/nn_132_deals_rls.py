"""Force tenant isolation on the root deals relation."""

from alembic import op

revision = "nn_132_deals_rls"
down_revision = "nn_131_deal_document_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE public.deals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.deals FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS deals_tenant ON public.deals")
    op.execute("""CREATE POLICY deals_tenant ON public.deals
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)""")
    op.execute("""COMMENT ON POLICY deals_tenant ON public.deals IS
        'Root deal boundary: reads and writes require transaction-local app.tenant_id.'""")


def downgrade() -> None:
    # Deliberately irreversible: a rollback must not reopen tenant deals.
    pass
