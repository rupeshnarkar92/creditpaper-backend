"""Force tenant isolation for document slots and their assigned files."""

from alembic import op

revision = "nn_126_document_slot_rls"
down_revision = "nn_125_onboarding_template_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("document_slots", "document_slot_files"):
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
