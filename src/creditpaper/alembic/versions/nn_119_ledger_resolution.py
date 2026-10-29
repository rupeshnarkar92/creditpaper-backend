"""Move ledger resolution inserts behind a stored function."""

from alembic import op

revision = "nn_119_ledger_resolution"
down_revision = "nn_118_ledger_supersession"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.record_ledger_resolution(p_tenant_id uuid,p_deal_id text,p_path text,p_assertion_id uuid,p_valid_from timestamptz,p_valid_to timestamptz,p_recorded_at timestamptz,p_actor_id uuid,p_reason text) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.ledger_resolutions(tenant_id,deal_id,field_path,assertion_id,valid_from,valid_to,recorded_at,actor_id,reason) VALUES(p_tenant_id,p_deal_id,p_path,p_assertion_id,p_valid_from,p_valid_to,p_recorded_at,p_actor_id,p_reason) $$; REVOKE ALL ON FUNCTION public.record_ledger_resolution(uuid,text,text,uuid,timestamptz,timestamptz,timestamptz,uuid,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.record_ledger_resolution(uuid,text,text,uuid,timestamptz,timestamptz,timestamptz,uuid,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.record_ledger_resolution(uuid,text,text,uuid,timestamptz,timestamptz,timestamptz,uuid,text)"
    )
