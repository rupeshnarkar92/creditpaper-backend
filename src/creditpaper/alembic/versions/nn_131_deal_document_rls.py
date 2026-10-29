"""Force tenant isolation for deal documents, versions, and extracted content."""

from alembic import op

revision = "nn_131_deal_document_rls"
down_revision = "nn_130_signal_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in (
        "deal_documents",
        "deal_document_versions",
        "deal_document_extractions",
    ):
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
            "'Restricts document metadata and extracted content to app.tenant_id.'"
        )


def downgrade() -> None:
    # Deliberately irreversible: a rollback must not reopen sensitive documents.
    pass
