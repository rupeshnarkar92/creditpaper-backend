"""Move ledger assertion deduplicating inserts behind a stored function."""

from alembic import op

revision = "nn_120_ledger_assert_insert"
down_revision = "nn_119_ledger_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.insert_ledger_assertion(p_tenant_id uuid,p_deal_id text,p_path text,p_fact jsonb,p_valid_from timestamptz,p_valid_to timestamptz,p_recorded_at timestamptz,p_version_id uuid,p_source_system text,p_run_id uuid,p_fingerprint text,p_disposition text) RETURNS uuid LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.ledger_assertions(tenant_id,deal_id,field_path,fact,valid_from,valid_to,recorded_at,source_version_id,source_system,run_id,fingerprint,disposition) VALUES(p_tenant_id,p_deal_id,p_path,p_fact,p_valid_from,p_valid_to,p_recorded_at,p_version_id,p_source_system,p_run_id,p_fingerprint,p_disposition) ON CONFLICT(tenant_id,deal_id,fingerprint) DO NOTHING RETURNING id $$; REVOKE ALL ON FUNCTION public.insert_ledger_assertion(uuid,text,text,jsonb,timestamptz,timestamptz,timestamptz,uuid,text,uuid,text,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.insert_ledger_assertion(uuid,text,text,jsonb,timestamptz,timestamptz,timestamptz,uuid,text,uuid,text,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.insert_ledger_assertion(uuid,text,text,jsonb,timestamptz,timestamptz,timestamptz,uuid,text,uuid,text,text)"
    )
