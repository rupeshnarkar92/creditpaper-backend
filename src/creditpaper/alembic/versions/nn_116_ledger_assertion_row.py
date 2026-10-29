"""Move ledger assertion decision lookup behind a stored function."""

from alembic import op

revision = "nn_116_ledger_assertion_row"
down_revision = "nn_115_ledger_recommendation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.ledger_assertion_row(p_tenant_id uuid,p_deal_id text,p_id uuid) RETURNS TABLE(field_path text,valid_from timestamptz,valid_to timestamptz,source_version_id uuid,fact jsonb) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT field_path,valid_from,valid_to,source_version_id,fact FROM public.ledger_assertions WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id AND id=p_id $$; REVOKE ALL ON FUNCTION public.ledger_assertion_row(uuid,text,uuid) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.ledger_assertion_row(uuid,text,uuid) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.ledger_assertion_row(uuid,text,uuid)")
