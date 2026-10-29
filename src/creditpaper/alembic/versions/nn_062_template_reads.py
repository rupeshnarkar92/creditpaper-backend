"""Move template and template-paper reads behind stored functions."""

from alembic import op

revision = "nn_062_template_reads"
down_revision = "nn_061_onboarding_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.tenant_templates(p_tenant_id uuid)
        RETURNS SETOF public.templates LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT t.* FROM public.templates t WHERE t.tenant_id=p_tenant_id ORDER BY t.is_default DESC,t.created_at DESC
        $$;
        CREATE OR REPLACE FUNCTION public.tenant_template(p_tenant_id uuid,p_id uuid)
        RETURNS SETOF public.templates LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT t.* FROM public.templates t WHERE t.tenant_id=p_tenant_id AND t.id=p_id
        $$;
        CREATE OR REPLACE FUNCTION public.default_template(p_tenant_id uuid)
        RETURNS SETOF public.templates LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT t.* FROM public.templates t WHERE t.tenant_id=p_tenant_id AND t.is_default=true LIMIT 1
        $$;
        CREATE OR REPLACE FUNCTION public.template_papers(p_tenant_id uuid,p_template_id uuid)
        RETURNS SETOF public.template_papers LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT p.* FROM public.template_papers p WHERE p.tenant_id=p_tenant_id AND p.template_id=p_template_id ORDER BY p.created_at DESC
        $$;
        CREATE OR REPLACE FUNCTION public.template_paper_count(p_tenant_id uuid,p_template_id uuid)
        RETURNS bigint LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT count(*) FROM public.template_papers p WHERE p.tenant_id=p_tenant_id AND p.template_id=p_template_id $$;
        CREATE OR REPLACE FUNCTION public.active_paper_types(p_only_active boolean)
        RETURNS SETOF public.paper_types LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT p.* FROM public.paper_types p WHERE (NOT p_only_active OR p.is_active=true) ORDER BY p.sort_order $$;
        REVOKE ALL ON FUNCTION public.tenant_templates(uuid),public.tenant_template(uuid,uuid),public.default_template(uuid),public.template_papers(uuid,uuid),public.template_paper_count(uuid,uuid),public.active_paper_types(boolean) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.tenant_templates(uuid),public.tenant_template(uuid,uuid),public.default_template(uuid),public.template_papers(uuid,uuid),public.template_paper_count(uuid,uuid),public.active_paper_types(boolean) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.tenant_templates(uuid),public.tenant_template(uuid,uuid),public.default_template(uuid),public.template_papers(uuid,uuid),public.template_paper_count(uuid,uuid),public.active_paper_types(boolean)"
    )
