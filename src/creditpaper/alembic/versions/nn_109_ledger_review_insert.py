"""Move ledger review-row inserts behind a stored function."""

from alembic import op

revision = "nn_109_ledger_review_insert"
down_revision = "nn_108_ledger_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.record_ledger_review(p_tenant_id uuid,p_deal_id text,p_assertion_id uuid,p_action text,p_actor_id uuid,p_reason text,p_recorded_at timestamptz) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.ledger_reviews(tenant_id,deal_id,assertion_id,action,actor_id,reason,recorded_at) VALUES(p_tenant_id,p_deal_id,p_assertion_id,p_action,p_actor_id,p_reason,p_recorded_at) $$; REVOKE ALL ON FUNCTION public.record_ledger_review(uuid,text,uuid,text,uuid,text,timestamptz) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.record_ledger_review(uuid,text,uuid,text,uuid,text,timestamptz) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.record_ledger_review(uuid,text,uuid,text,uuid,text,timestamptz)"
    )
