"""Move direct ledger supersession inserts behind a stored function."""

from alembic import op

revision = "nn_118_ledger_supersession"
down_revision = "nn_117_ledger_overlaps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.record_ledger_supersession(p_tenant_id uuid,p_deal_id text,p_resolution_id uuid,p_recorded_at timestamptz,p_actor_id uuid,p_reason text) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.ledger_supersessions(tenant_id,deal_id,resolution_id,recorded_at,actor_id,reason) VALUES(p_tenant_id,p_deal_id,p_resolution_id,p_recorded_at,p_actor_id,p_reason) $$; REVOKE ALL ON FUNCTION public.record_ledger_supersession(uuid,text,uuid,timestamptz,uuid,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.record_ledger_supersession(uuid,text,uuid,timestamptz,uuid,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.record_ledger_supersession(uuid,text,uuid,timestamptz,uuid,text)"
    )
