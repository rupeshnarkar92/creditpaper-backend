"""Move latest signed recommendation lookup behind a stored function."""

from alembic import op

revision = "nn_115_ledger_recommendation"
down_revision = "nn_114_ledger_review_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.current_deal_recommendation(p_tenant_id uuid,p_deal_id text) RETURNS TABLE(id uuid,author_id uuid,signed_at timestamptz,body text) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT id,author_id,signed_at,body FROM public.deal_recommendations WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id ORDER BY signed_at DESC,id DESC LIMIT 1 $$; REVOKE ALL ON FUNCTION public.current_deal_recommendation(uuid,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.current_deal_recommendation(uuid,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.current_deal_recommendation(uuid,text)")
