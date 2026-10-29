"""Move onboarding paper create/delete behind stored functions."""

from alembic import op

revision = "nn_095_paper_create_delete"
down_revision = "nn_094_template_change"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.create_onboarding_paper(p_id text,p_tenant_id uuid,p_name text,p_deal_type text,p_borrower text,p_sector text,p_address text,p_stage text,p_thesis text,p_thesis_status text) RETURNS SETOF public.deals LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.deals(id,tenant_id,name,deal_type,borrower,sector,address,thesis_content,thesis_status) VALUES(p_id,p_tenant_id,p_name,p_deal_type,p_borrower,p_sector,p_address,p_thesis,p_thesis_status) RETURNING * $$; CREATE OR REPLACE FUNCTION public.delete_onboarding_paper(p_tenant_id uuid,p_id text) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ WITH d AS (DELETE FROM public.deals WHERE tenant_id=p_tenant_id AND id=p_id RETURNING 1) SELECT EXISTS(SELECT 1 FROM d) $$; REVOKE ALL ON FUNCTION public.create_onboarding_paper(text,uuid,text,text,text,text,text,text,text,text),public.delete_onboarding_paper(uuid,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.create_onboarding_paper(text,uuid,text,text,text,text,text,text,text,text),public.delete_onboarding_paper(uuid,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.create_onboarding_paper(text,uuid,text,text,text,text,text,text,text,text),public.delete_onboarding_paper(uuid,text)"
    )
