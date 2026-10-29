"""Move slot document/version listing behind a stored function."""

from alembic import op

revision = "nn_098_slot_documents"
down_revision = "nn_097_paper_update"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.slot_document_versions(p_tenant_id uuid,p_deal_id text) RETURNS TABLE(document_id uuid,display_name text,document_kind text,document_created_at timestamptz,version_id uuid,version_number integer,original_filename text,size_bytes bigint,uploaded_at timestamptz,processing_status text,extraction_status text,extraction_error text,superseded_at timestamptz) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT d.id,d.display_name,d.document_kind,d.created_at,v.id,v.version_number,v.original_filename,v.size_bytes,v.uploaded_at,v.processing_status,v.extraction_status,v.extraction_error,v.superseded_at FROM public.deal_documents d JOIN public.deal_document_versions v ON v.document_id=d.id WHERE d.tenant_id=p_tenant_id AND d.deal_id=p_deal_id AND d.archived_at IS NULL ORDER BY d.created_at,v.version_number DESC $$; REVOKE ALL ON FUNCTION public.slot_document_versions(uuid,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.slot_document_versions(uuid,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.slot_document_versions(uuid,text)")
