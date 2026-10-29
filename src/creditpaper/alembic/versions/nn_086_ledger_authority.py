"""Move authority-choice assertion selection into a stored function."""

from alembic import op

revision = "nn_086_ledger_authority"
down_revision = "nn_085_ledger_peers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.ledger_authority_assertions(p_tenant_id uuid,p_deal_id text,p_assertion_ids uuid[]) RETURNS TABLE(assertion_id text,fact jsonb) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT id::text,fact FROM public.ledger_assertions WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id AND id=ANY(p_assertion_ids) ORDER BY recorded_at,id $$; REVOKE ALL ON FUNCTION public.ledger_authority_assertions(uuid,text,uuid[]) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.ledger_authority_assertions(uuid,text,uuid[]) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.ledger_authority_assertions(uuid,text,uuid[])")
