"""Move validated onboarding document count into a stored function."""

from alembic import op

revision = "nn_087_onboarding_count"
down_revision = "nn_086_ledger_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.validated_onboarding_count(p_tenant_id uuid) RETURNS bigint LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT count(*) FROM public.onboarding_documents WHERE tenant_id=p_tenant_id AND status='validated' AND approval_detected=true $$; REVOKE ALL ON FUNCTION public.validated_onboarding_count(uuid) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.validated_onboarding_count(uuid) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.validated_onboarding_count(uuid)")
