"""Move ledger model snapshot persistence behind a stored function."""

from alembic import op

revision = "nn_107_ledger_snapshot"
down_revision = "nn_106_ledger_revision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.save_ledger_snapshot(p_tenant_id uuid,p_deal_id text,p_model jsonb) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.deal_model_snapshots(tenant_id,deal_id,model) VALUES(p_tenant_id,p_deal_id,p_model) ON CONFLICT(deal_id) DO UPDATE SET model=EXCLUDED.model,created_at=now() WHERE deal_model_snapshots.tenant_id=EXCLUDED.tenant_id $$; REVOKE ALL ON FUNCTION public.save_ledger_snapshot(uuid,text,jsonb) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.save_ledger_snapshot(uuid,text,jsonb) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.save_ledger_snapshot(uuid,text,jsonb)")
