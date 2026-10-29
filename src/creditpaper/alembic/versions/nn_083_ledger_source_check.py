"""Move ledger source validity checks into a stored function."""

from alembic import op

revision = "nn_083_ledger_source_check"
down_revision = "nn_082_ledger_review_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.ledger_source_is_current(p_tenant_id uuid,p_deal_id text,p_version_id uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT EXISTS(SELECT 1 FROM public.deal_document_versions v JOIN public.deal_documents d ON d.id=v.document_id WHERE v.id=p_version_id AND v.tenant_id=p_tenant_id AND v.deal_id=p_deal_id AND v.superseded_at IS NULL AND d.archived_at IS NULL) $$; REVOKE ALL ON FUNCTION public.ledger_source_is_current(uuid,text,uuid) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.ledger_source_is_current(uuid,text,uuid) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.ledger_source_is_current(uuid,text,uuid)")
