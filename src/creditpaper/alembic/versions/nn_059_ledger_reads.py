"""Move temporal ledger read queries behind stored functions."""

from alembic import op

revision = "nn_059_ledger_reads"
down_revision = "nn_058_deal_template_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.ledger_deal_exists(p_tenant_id uuid,p_deal_id text)
        RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT EXISTS (SELECT 1 FROM public.deals d WHERE d.tenant_id=p_tenant_id AND d.id=p_deal_id)
        $$;
        CREATE OR REPLACE FUNCTION public.ledger_history_rows(
            p_tenant_id uuid,p_deal_id text,p_known_at timestamptz,p_valid_at timestamptz,p_include_undated boolean)
        RETURNS TABLE(id uuid,assertion_id uuid,field_path text,fact jsonb,valid_from timestamptz,valid_to timestamptz,
                      recorded_at timestamptz,superseded_at timestamptz,effective_date_unknown boolean,human_verified boolean)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT h.id,h.assertion_id,h.field_path,h.fact,h.valid_from,h.valid_to,h.recorded_at,h.superseded_at,
              h.effective_date_unknown,EXISTS(SELECT 1 FROM public.ledger_reviews v WHERE v.tenant_id=h.tenant_id
                AND v.assertion_id=h.assertion_id AND v.action='accept' AND v.recorded_at<=p_known_at)
            FROM public.ledger_fact_history h WHERE h.tenant_id=p_tenant_id AND h.deal_id=p_deal_id
              AND h.recorded_at<=p_known_at AND (h.superseded_at IS NULL OR h.superseded_at>p_known_at)
              AND h.valid_period @> p_valid_at AND (p_include_undated OR NOT h.effective_date_unknown)
            ORDER BY h.field_path
        $$;
        CREATE OR REPLACE FUNCTION public.ledger_revision(p_tenant_id uuid,p_deal_id text)
        RETURNS bigint LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT d.ledger_revision FROM public.deals d WHERE d.tenant_id=p_tenant_id AND d.id=p_deal_id
        $$;
        CREATE OR REPLACE FUNCTION public.ledger_outbox_revision(p_tenant_id uuid,p_deal_id text,p_known_at timestamptz)
        RETURNS bigint LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT coalesce(max((o.payload->>'revision')::bigint),0) FROM public.ledger_outbox o
            WHERE o.tenant_id=p_tenant_id AND o.deal_id=p_deal_id AND o.recorded_at<=p_known_at
        $$;
        CREATE OR REPLACE FUNCTION public.legacy_deal_snapshot(p_tenant_id uuid,p_deal_id text)
        RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT s.model FROM public.deal_model_snapshots s WHERE s.tenant_id=p_tenant_id AND s.deal_id=p_deal_id
        $$;
        REVOKE ALL ON FUNCTION public.ledger_deal_exists(uuid,text),public.ledger_history_rows(uuid,text,timestamptz,timestamptz,boolean),public.ledger_revision(uuid,text),public.ledger_outbox_revision(uuid,text,timestamptz),public.legacy_deal_snapshot(uuid,text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.ledger_deal_exists(uuid,text),public.ledger_history_rows(uuid,text,timestamptz,timestamptz,boolean),public.ledger_revision(uuid,text),public.ledger_outbox_revision(uuid,text,timestamptz),public.legacy_deal_snapshot(uuid,text) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.ledger_deal_exists(uuid,text),public.ledger_history_rows(uuid,text,timestamptz,timestamptz,boolean),public.ledger_revision(uuid,text),public.ledger_outbox_revision(uuid,text,timestamptz),public.legacy_deal_snapshot(uuid,text)"
    )
