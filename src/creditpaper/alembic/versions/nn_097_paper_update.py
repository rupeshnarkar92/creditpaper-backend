"""Move onboarding paper updates behind a typed stored function."""

from alembic import op

revision = "nn_097_paper_update"
down_revision = "nn_096_paper_exists"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.update_onboarding_paper(p_tenant_id uuid,p_id text,p_name text,p_deal_type text,p_borrower text,p_sector text,p_address text,p_thesis text,p_thesis_status text) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ WITH u AS (UPDATE public.deals SET name=COALESCE(p_name,name),deal_type=COALESCE(p_deal_type,deal_type),borrower=COALESCE(p_borrower,borrower),sector=COALESCE(p_sector,sector),address=COALESCE(p_address,address),thesis_content=COALESCE(p_thesis,thesis_content),thesis_status=COALESCE(p_thesis_status,thesis_status),updated_at=now() WHERE tenant_id=p_tenant_id AND id=p_id RETURNING 1) SELECT EXISTS(SELECT 1 FROM u) $$; REVOKE ALL ON FUNCTION public.update_onboarding_paper(uuid,text,text,text,text,text,text,text,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.update_onboarding_paper(uuid,text,text,text,text,text,text,text,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.update_onboarding_paper(uuid,text,text,text,text,text,text,text,text)"
    )
