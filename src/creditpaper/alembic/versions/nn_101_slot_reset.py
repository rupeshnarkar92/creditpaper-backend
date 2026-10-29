"""Move extraction reset updates behind a stored function."""

from alembic import op

revision = "nn_101_slot_reset"
down_revision = "nn_100_slot_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.reset_document_extraction(p_tenant_id uuid,p_deal_id text,p_version_id uuid) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ WITH u AS (UPDATE public.deal_document_versions SET extraction_status='processing',extraction_error=NULL WHERE id=p_version_id AND tenant_id=p_tenant_id AND deal_id=p_deal_id RETURNING 1) SELECT EXISTS(SELECT 1 FROM u) $$; REVOKE ALL ON FUNCTION public.reset_document_extraction(uuid,text,uuid) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.reset_document_extraction(uuid,text,uuid) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.reset_document_extraction(uuid,text,uuid)")
