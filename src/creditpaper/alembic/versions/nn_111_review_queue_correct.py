"""Move corrected fact-review queue updates behind a stored function."""

from alembic import op

revision = "nn_111_review_queue_correct"
down_revision = "nn_110_review_queue_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION public.correct_fact_review(
      p_tenant_id uuid,p_deal_id text,p_queue_id uuid,p_value jsonb,
      p_reviewer uuid,p_reviewed_at timestamptz,p_notes text
    ) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      WITH u AS (
        UPDATE public.fact_review_queue SET status='corrected',reviewed_value=p_value,
          reviewed_by=p_reviewer,reviewed_at=p_reviewed_at,reviewer_notes=p_notes
        WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id AND id=p_queue_id RETURNING 1
      ) SELECT EXISTS(SELECT 1 FROM u)
    $$;
    REVOKE ALL ON FUNCTION public.correct_fact_review(uuid,text,uuid,jsonb,uuid,timestamptz,text) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.correct_fact_review(uuid,text,uuid,jsonb,uuid,timestamptz,text) TO creditpaper_runtime;
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.correct_fact_review(uuid,text,uuid,jsonb,uuid,timestamptz,text)"
    )
