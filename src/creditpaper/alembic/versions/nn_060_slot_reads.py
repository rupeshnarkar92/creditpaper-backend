"""Move slot board read queries behind stored functions."""

from alembic import op

revision = "nn_060_slot_reads"
down_revision = "nn_059_ledger_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.platform_slot_types()
        RETURNS TABLE(key text,name text,description text,category text,applies_to text[],optional_for text[],multi boolean,extractor text,model_block text,checks jsonb,sort_order integer)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT t.key,t.name,t.description,t.category,t.applies_to,t.optional_for,t.multi,t.extractor,t.model_block,t.checks,t.sort_order
            FROM public.document_slot_types t ORDER BY t.sort_order
        $$;
        CREATE OR REPLACE FUNCTION public.platform_refused_classes()
        RETURNS TABLE(key text,name text,reason text,match text[],decision text)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT r.key,r.name,r.reason,r.match,r.decision FROM public.document_refused_classes r ORDER BY r.key
        $$;
        CREATE OR REPLACE FUNCTION public.slot_deal_row(p_tenant_id uuid,p_deal_id text)
        RETURNS TABLE(id text,deal_type text) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT d.id,d.deal_type FROM public.deals d WHERE d.id=p_deal_id AND d.tenant_id=p_tenant_id
        $$;
        CREATE OR REPLACE FUNCTION public.slot_snapshot(p_tenant_id uuid,p_deal_id text)
        RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT s.model FROM public.deal_model_snapshots s WHERE s.deal_id=p_deal_id AND s.tenant_id=p_tenant_id
        $$;
        CREATE OR REPLACE FUNCTION public.version_processing_status(p_tenant_id uuid,p_version_id uuid)
        RETURNS text LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT v.processing_status FROM public.deal_document_versions v WHERE v.id=p_version_id AND v.tenant_id=p_tenant_id
        $$;
        REVOKE ALL ON FUNCTION public.platform_slot_types(),public.platform_refused_classes(),public.slot_deal_row(uuid,text),public.slot_snapshot(uuid,text),public.version_processing_status(uuid,uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.platform_slot_types(),public.platform_refused_classes(),public.slot_deal_row(uuid,text),public.slot_snapshot(uuid,text),public.version_processing_status(uuid,uuid) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.platform_slot_types(),public.platform_refused_classes(),public.slot_deal_row(uuid,text),public.slot_snapshot(uuid,text),public.version_processing_status(uuid,uuid)"
    )
