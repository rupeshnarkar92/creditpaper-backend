"""Move ledger lock and clock primitives into stored functions."""

from alembic import op

revision = "nn_079_ledger_primitives"
down_revision = "nn_078_drop_held_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.lock_ledger_deal(p_tenant_id uuid,p_deal_id text) RETURNS integer LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT ledger_revision FROM public.deals WHERE tenant_id=p_tenant_id AND id=p_deal_id FOR UPDATE $$; CREATE OR REPLACE FUNCTION public.ledger_clock() RETURNS timestamptz LANGUAGE sql VOLATILE SECURITY INVOKER SET search_path=pg_catalog AS $$ SELECT clock_timestamp() $$; REVOKE ALL ON FUNCTION public.lock_ledger_deal(uuid,text),public.ledger_clock() FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.lock_ledger_deal(uuid,text),public.ledger_clock() TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.lock_ledger_deal(uuid,text), public.ledger_clock()")
