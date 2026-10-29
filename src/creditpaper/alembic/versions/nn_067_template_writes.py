"""Move template create/delete and paper writes behind stored functions."""

from alembic import op

revision = "nn_067_template_writes"
down_revision = "nn_066_guardrail_writes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.create_template(p_id uuid,p_tenant_id uuid,p_name text,p_description text,p_paper_types jsonb,p_sections jsonb,p_default boolean,p_active boolean,p_source text)
        RETURNS SETOF public.templates LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.templates(id,tenant_id,name,description,paper_types,sections,is_default,is_active,source) VALUES(p_id,p_tenant_id,p_name,p_description,p_paper_types,p_sections,p_default,p_active,p_source) RETURNING * $$;
        CREATE OR REPLACE FUNCTION public.delete_template(p_tenant_id uuid,p_id uuid) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ DELETE FROM public.templates WHERE tenant_id=p_tenant_id AND id=p_id RETURNING true $$;
        CREATE OR REPLACE FUNCTION public.add_template_paper(p_id uuid,p_template_id uuid,p_tenant_id uuid,p_filename text,p_storage_path text,p_credit boolean,p_approval boolean,p_confidence double precision,p_size bigint,p_content_type text,p_sections jsonb,p_tags jsonb)
        RETURNS SETOF public.template_papers LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.template_papers(id,template_id,tenant_id,filename,storage_path,is_credit_paper,approval_detected,confidence,size,content_type,extracted_sections,tags) VALUES(p_id,p_template_id,p_tenant_id,p_filename,p_storage_path,p_credit,p_approval,p_confidence,p_size,p_content_type,p_sections,p_tags) RETURNING * $$;
        CREATE OR REPLACE FUNCTION public.delete_template_paper(p_tenant_id uuid,p_template_id uuid,p_id uuid) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ DELETE FROM public.template_papers WHERE tenant_id=p_tenant_id AND template_id=p_template_id AND id=p_id RETURNING true $$;
        REVOKE ALL ON FUNCTION public.create_template(uuid,uuid,text,text,jsonb,jsonb,boolean,boolean,text),public.delete_template(uuid,uuid),public.add_template_paper(uuid,uuid,uuid,text,text,boolean,boolean,double precision,bigint,text,jsonb,jsonb),public.delete_template_paper(uuid,uuid,uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.create_template(uuid,uuid,text,text,jsonb,jsonb,boolean,boolean,text),public.delete_template(uuid,uuid),public.add_template_paper(uuid,uuid,uuid,text,text,boolean,boolean,double precision,bigint,text,jsonb,jsonb),public.delete_template_paper(uuid,uuid,uuid) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.create_template(uuid,uuid,text,text,jsonb,jsonb,boolean,boolean,text),public.delete_template(uuid,uuid),public.add_template_paper(uuid,uuid,uuid,text,text,boolean,boolean,double precision,bigint,text,jsonb,jsonb),public.delete_template_paper(uuid,uuid,uuid)"
    )
