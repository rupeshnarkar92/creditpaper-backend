"""Move onboarding paper existence checks behind a stored function."""

from alembic import op

revision = "nn_096_paper_exists"
down_revision = "nn_095_paper_create_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.onboarding_paper_exists(p_tenant_id uuid,p_id text) RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT EXISTS(SELECT 1 FROM public.deals WHERE tenant_id=p_tenant_id AND id=p_id) $$; REVOKE ALL ON FUNCTION public.onboarding_paper_exists(uuid,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.onboarding_paper_exists(uuid,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.onboarding_paper_exists(uuid,text)")
