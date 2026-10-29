"""Move fact-review queue status updates behind a stored function."""

from alembic import op

revision = "nn_110_review_queue_status"
down_revision = "nn_109_ledger_review_insert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.update_fact_review_status(p_tenant_id uuid,p_deal_id text,p_assertion_id uuid,p_status text,p_reviewer uuid,p_reviewed_at timestamptz,p_notes text) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ WITH u AS (UPDATE public.fact_review_queue SET status=p_status,reviewed_by=p_reviewer,reviewed_at=p_reviewed_at,reviewer_notes=p_notes WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id AND assertion_id=p_assertion_id RETURNING 1) SELECT EXISTS(SELECT 1 FROM u) $$; REVOKE ALL ON FUNCTION public.update_fact_review_status(uuid,text,uuid,text,uuid,timestamptz,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.update_fact_review_status(uuid,text,uuid,text,uuid,timestamptz,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.update_fact_review_status(uuid,text,uuid,text,uuid,timestamptz,text)"
    )
