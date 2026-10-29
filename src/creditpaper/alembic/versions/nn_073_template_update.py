"""Move template partial updates behind a typed stored function."""

from alembic import op

revision = "nn_073_template_update"
down_revision = "nn_072_guardrail_update"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.update_template(p_tenant_id uuid,p_id uuid,p_name text,p_description text,p_paper_types jsonb,p_sections jsonb,p_active boolean)
        RETURNS SETOF public.templates LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            UPDATE public.templates SET name=COALESCE(p_name,name),description=COALESCE(p_description,description),paper_types=COALESCE(p_paper_types,paper_types),sections=COALESCE(p_sections,sections),is_active=COALESCE(p_active,is_active),updated_at=now()
            WHERE tenant_id=p_tenant_id AND id=p_id RETURNING *
        $$;
        REVOKE ALL ON FUNCTION public.update_template(uuid,uuid,text,text,jsonb,jsonb,boolean) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.update_template(uuid,uuid,text,text,jsonb,jsonb,boolean) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.update_template(uuid,uuid,text,text,jsonb,jsonb,boolean)")
