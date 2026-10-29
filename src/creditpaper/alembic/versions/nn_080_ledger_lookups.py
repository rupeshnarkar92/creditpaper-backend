"""Move ledger assertion and snapshot lookups into stored functions."""

from alembic import op

revision = "nn_080_ledger_lookups"
down_revision = "nn_079_ledger_primitives"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.ledger_assertion_by_fingerprint(p_tenant_id uuid,p_deal_id text,p_fingerprint text) RETURNS uuid LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT id FROM public.ledger_assertions WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id AND fingerprint=p_fingerprint LIMIT 1 $$; CREATE OR REPLACE FUNCTION public.ledger_has_assertions(p_tenant_id uuid,p_deal_id text) RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT EXISTS(SELECT 1 FROM public.ledger_assertions WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id) $$; CREATE OR REPLACE FUNCTION public.legacy_model_snapshot(p_tenant_id uuid,p_deal_id text) RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT model FROM public.deal_model_snapshots WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id LIMIT 1 $$; REVOKE ALL ON FUNCTION public.ledger_assertion_by_fingerprint(uuid,text,text),public.ledger_has_assertions(uuid,text),public.legacy_model_snapshot(uuid,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.ledger_assertion_by_fingerprint(uuid,text,text),public.ledger_has_assertions(uuid,text),public.legacy_model_snapshot(uuid,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.ledger_assertion_by_fingerprint(uuid,text,text),public.ledger_has_assertions(uuid,text),public.legacy_model_snapshot(uuid,text)"
    )
