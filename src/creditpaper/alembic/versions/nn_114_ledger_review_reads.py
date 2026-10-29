"""Move ledger review queue reads behind stored functions."""

from alembic import op

revision = "nn_114_ledger_review_reads"
down_revision = "nn_113_ledger_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.lock_fact_review_item(p_tenant_id uuid,p_deal_id text,p_id uuid) RETURNS TABLE(assertion_id uuid,status text,field_path text) LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT assertion_id,status,field_path FROM public.fact_review_queue WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id AND id=p_id FOR UPDATE $$; CREATE OR REPLACE FUNCTION public.ledger_assertion_fact(p_tenant_id uuid,p_deal_id text,p_id uuid) RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT fact FROM public.ledger_assertions WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id AND id=p_id $$; REVOKE ALL ON FUNCTION public.lock_fact_review_item(uuid,text,uuid),public.ledger_assertion_fact(uuid,text,uuid) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.lock_fact_review_item(uuid,text,uuid),public.ledger_assertion_fact(uuid,text,uuid) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.lock_fact_review_item(uuid,text,uuid),public.ledger_assertion_fact(uuid,text,uuid)"
    )
