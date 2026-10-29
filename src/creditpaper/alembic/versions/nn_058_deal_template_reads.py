"""Move deal template read queries behind stored functions."""

from alembic import op

revision = "nn_058_deal_template_reads"
down_revision = "nn_057_integration_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.deal_template_row(p_tenant_id uuid,p_deal_id text)
        RETURNS TABLE(id text,deal_type text,template_id uuid,template_source text,template_snapshot jsonb,has_paper_sections boolean)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT d.id,d.deal_type,d.template_id,d.template_source,d.template_snapshot,
              (EXISTS (SELECT 1 FROM public.deal_paper_sections s WHERE s.deal_id=d.id)
               OR EXISTS (SELECT 1 FROM public.processing_jobs j WHERE j.deal_id=d.id AND j.tenant_id=d.tenant_id
                          AND j.kind='generation' AND j.status IN ('pending','running')))
            FROM public.deals d WHERE d.id=p_deal_id AND d.tenant_id=p_tenant_id
        $$;
        CREATE OR REPLACE FUNCTION public.deal_paper_section_rows(p_deal_id text)
        RETURNS TABLE(section integer,status text,prose text,citations jsonb,error text,editor_value text,edit_revision bigint)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT s.section,s.status,s.prose,s.citations,s.error,s.editor_value,s.edit_revision
            FROM public.deal_paper_sections s WHERE s.deal_id=p_deal_id ORDER BY s.section
        $$;
        CREATE OR REPLACE FUNCTION public.active_generation_job(p_tenant_id uuid,p_deal_id text)
        RETURNS TABLE(id uuid,status text) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT j.id,j.status FROM public.processing_jobs j WHERE j.tenant_id=p_tenant_id AND j.deal_id=p_deal_id
              AND j.kind='generation' AND j.status IN ('pending','running') ORDER BY j.created_at LIMIT 1
        $$;
        REVOKE ALL ON FUNCTION public.deal_template_row(uuid,text),public.deal_paper_section_rows(text),public.active_generation_job(uuid,text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.deal_template_row(uuid,text),public.deal_paper_section_rows(text),public.active_generation_job(uuid,text) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.deal_template_row(uuid,text),public.deal_paper_section_rows(text),public.active_generation_job(uuid,text)"
    )
