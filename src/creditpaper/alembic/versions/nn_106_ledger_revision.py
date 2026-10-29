"""Move ledger revision bump behind a stored function."""

from alembic import op

revision = "nn_106_ledger_revision"
down_revision = "nn_105_integration_quota"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.bump_ledger_revision(p_tenant_id uuid,p_deal_id text) RETURNS integer LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ UPDATE public.deals SET ledger_revision=ledger_revision+1 WHERE tenant_id=p_tenant_id AND id=p_deal_id RETURNING ledger_revision $$; REVOKE ALL ON FUNCTION public.bump_ledger_revision(uuid,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.bump_ledger_revision(uuid,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.bump_ledger_revision(uuid,text)")
