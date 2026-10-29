"""Move confirmed-slot lookup behind a stored function."""

from alembic import op

revision = "nn_099_slot_confirmed"
down_revision = "nn_098_slot_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.confirmed_slot_keys(p_tenant_id uuid,p_deal_ids text[]) RETURNS TABLE(deal_id text,slot_key text) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT s.deal_id,s.slot_key FROM public.document_slots s JOIN public.document_slot_files f ON f.slot_id=s.id AND f.role='assigned' AND f.tenant_id=s.tenant_id JOIN public.deal_documents d ON d.id=f.document_id AND d.archived_at IS NULL JOIN public.deal_document_versions v ON v.id=f.version_id WHERE s.tenant_id=p_tenant_id AND s.deal_id=ANY(p_deal_ids) AND s.status IN('confirmed','processing') GROUP BY s.deal_id,s.slot_key,s.status HAVING s.status='confirmed' OR bool_and(v.extraction_status='ready') $$; REVOKE ALL ON FUNCTION public.confirmed_slot_keys(uuid,text[]) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.confirmed_slot_keys(uuid,text[]) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.confirmed_slot_keys(uuid,text[])")
