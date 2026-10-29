"""Move ledger outbox writes behind a stored function."""

from alembic import op

revision = "nn_108_ledger_outbox"
down_revision = "nn_107_ledger_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.enqueue_ledger_event(p_tenant_id uuid,p_deal_id text,p_event_type text,p_payload jsonb) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.ledger_outbox(tenant_id,deal_id,event_type,payload) VALUES(p_tenant_id,p_deal_id,p_event_type,p_payload) $$; REVOKE ALL ON FUNCTION public.enqueue_ledger_event(uuid,text,text,jsonb) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.enqueue_ledger_event(uuid,text,text,jsonb) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.enqueue_ledger_event(uuid,text,text,jsonb)")
