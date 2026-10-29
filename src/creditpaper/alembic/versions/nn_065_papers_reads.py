"""Move onboarding paper read queries behind stored functions."""

from alembic import op

revision = "nn_065_papers_reads"
down_revision = "nn_064_intake_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.onboarding_papers(p_tenant_id uuid)
        RETURNS SETOF public.deals LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT d.* FROM public.deals d WHERE d.tenant_id=p_tenant_id ORDER BY d.created_at $$;
        CREATE OR REPLACE FUNCTION public.onboarding_paper(p_tenant_id uuid,p_id text)
        RETURNS SETOF public.deals LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT d.* FROM public.deals d WHERE d.tenant_id=p_tenant_id AND d.id=p_id $$;
        CREATE OR REPLACE FUNCTION public.onboarding_model_snapshot(p_tenant_id uuid,p_id text)
        RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT s.model FROM public.deal_model_snapshots s WHERE s.tenant_id=p_tenant_id AND s.deal_id=p_id $$;
        CREATE OR REPLACE FUNCTION public.onboarding_deal_events(p_tenant_id uuid,p_id text)
        RETURNS TABLE(stage text,status text,message text,metadata jsonb,created_at timestamptz) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT e.stage,e.status,e.message,e.metadata,e.created_at FROM public.deal_events e WHERE e.tenant_id=p_tenant_id AND e.deal_id=p_id ORDER BY e.created_at $$;
        REVOKE ALL ON FUNCTION public.onboarding_papers(uuid),public.onboarding_paper(uuid,text),public.onboarding_model_snapshot(uuid,text),public.onboarding_deal_events(uuid,text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.onboarding_papers(uuid),public.onboarding_paper(uuid,text),public.onboarding_model_snapshot(uuid,text),public.onboarding_deal_events(uuid,text) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.onboarding_papers(uuid),public.onboarding_paper(uuid,text),public.onboarding_model_snapshot(uuid,text),public.onboarding_deal_events(uuid,text)"
    )
