"""Force tenant isolation for deal audit and lifecycle event tables."""

from alembic import op

revision = "nn_129_deal_event_rls"
down_revision = "nn_128_inactive_artifact_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("deal_audit_events", "deal_events", "deal_state_transitions"):
        policy = f"{table}_tenant"
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {policy} ON public.{table}")
        op.execute(
            f"""CREATE POLICY {policy} ON public.{table}
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)"""
        )
        op.execute(
            f"COMMENT ON POLICY {policy} ON public.{table} IS "
            "'Restricts lifecycle and audit events to the transaction-local app.tenant_id.'"
        )


def downgrade() -> None:
    # Deliberately irreversible: a rollback must not reopen tenant audit data.
    pass
