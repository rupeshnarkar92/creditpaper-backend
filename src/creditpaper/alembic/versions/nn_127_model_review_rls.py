"""Force tenant isolation for model snapshots and fact review work."""

from alembic import op

revision = "nn_127_model_review_rls"
down_revision = "nn_126_document_slot_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("deal_model_snapshots", "fact_review_queue"):
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
            "'Restricts reads and writes to the transaction-local app.tenant_id.'"
        )


def downgrade() -> None:
    # Deliberately irreversible: a rollback must not reopen tenant data.
    pass
