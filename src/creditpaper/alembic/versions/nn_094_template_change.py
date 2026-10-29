"""Move guarded deal-template changes behind a stored function."""

from alembic import op

revision = "nn_094_template_change"
down_revision = "nn_093_guardrail_accuracy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.change_deal_template(p_tenant_id uuid,p_deal_id text,p_template_id uuid,p_source text,p_snapshot jsonb) RETURNS text LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ DECLARE ok boolean; BEGIN PERFORM 1 FROM public.deals WHERE id=p_deal_id AND tenant_id=p_tenant_id FOR UPDATE; IF NOT FOUND THEN RETURN 'not_found'; END IF; UPDATE public.deals SET template_id=p_template_id,template_source=p_source,template_snapshot=p_snapshot,updated_at=now() WHERE id=p_deal_id AND tenant_id=p_tenant_id AND NOT EXISTS(SELECT 1 FROM public.deal_paper_sections WHERE deal_id=p_deal_id) AND NOT EXISTS(SELECT 1 FROM public.processing_jobs j WHERE j.deal_id=p_deal_id AND j.tenant_id=p_tenant_id AND j.kind='generation' AND j.status IN('pending','running')); IF FOUND THEN RETURN 'ok'; END IF; SELECT true INTO ok FROM public.deals WHERE id=p_deal_id AND tenant_id=p_tenant_id; RETURN CASE WHEN ok THEN 'locked' ELSE 'not_found' END; END $$; REVOKE ALL ON FUNCTION public.change_deal_template(uuid,text,uuid,text,jsonb) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.change_deal_template(uuid,text,uuid,text,jsonb) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.change_deal_template(uuid,text,uuid,text,jsonb)")
