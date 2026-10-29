"""Move ledger temporal conflict check into a stored function."""

from alembic import op

revision = "nn_084_ledger_history_check"
down_revision = "nn_083_ledger_source_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.ledger_history_conflict(p_tenant_id uuid,p_deal_id text,p_path text,p_valid_from timestamptz,p_valid_to timestamptz,p_value jsonb,p_same_source boolean,p_version_id uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT EXISTS(SELECT 1 FROM public.ledger_fact_history h WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id AND field_path=p_path AND superseded_at IS NULL AND valid_period && tstzrange(p_valid_from,p_valid_to,'[)') AND fact->'value' IS DISTINCT FROM p_value AND (p_same_source OR source_version_id IS NOT DISTINCT FROM p_version_id)) $$; REVOKE ALL ON FUNCTION public.ledger_history_conflict(uuid,text,text,timestamptz,timestamptz,jsonb,boolean,uuid) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.ledger_history_conflict(uuid,text,text,timestamptz,timestamptz,jsonb,boolean,uuid) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.ledger_history_conflict(uuid,text,text,timestamptz,timestamptz,jsonb,boolean,uuid)"
    )
