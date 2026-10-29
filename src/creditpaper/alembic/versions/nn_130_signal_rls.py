"""Fail closed for the legacy tenant signals table."""

from alembic import op

revision = "nn_130_signal_rls"
down_revision = "nn_129_deal_event_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE public.signals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.signals FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS signals_tenant ON public.signals")
    op.execute("""CREATE POLICY signals_tenant ON public.signals
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)""")
    op.execute("""COMMENT ON POLICY signals_tenant ON public.signals IS
        'Fail-closed tenant boundary for the legacy signals relation.'""")


def downgrade() -> None:
    # Deliberately irreversible: a rollback must not reopen tenant data.
    pass
