"""Prevent a native-parse backlog from hiding ready work in other lanes."""

from alembic import op

revision = "US_delivery_lane_fairness"
down_revision = "US_processing_delivery_intent"
branch_labels = None
depends_on = None

_SQL = """
CREATE OR REPLACE FUNCTION public.pending_processing_jobs_for_delivery(
          p_tenant uuid,p_kinds text[],p_limit integer DEFAULT 25
        ) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          PERFORM public.sweep_processing_job_expired_leases(p_tenant);
          RETURN (
          WITH ranked AS (
            SELECT id,kind,deal_id,created_at,
                   row_number() OVER (PARTITION BY deal_id,kind ORDER BY created_at,id) AS deal_rank
            FROM public.processing_jobs
            WHERE tenant_id=p_tenant AND attempts<3
              AND (p_kinds IS NULL OR kind=ANY(p_kinds))
              AND (
                (status='pending' AND available_at<=clock_timestamp())
                OR (status='running' AND lease_until<=clock_timestamp())
              )
          ), due AS (
            SELECT id,kind,created_at,deal_rank FROM ranked
            ORDER BY deal_rank,created_at,id
            LIMIT GREATEST(1,LEAST(p_limit,250))
          )
          SELECT COALESCE(
            jsonb_agg(jsonb_build_object('id',id,'kind',kind)
                      ORDER BY deal_rank,created_at,id),
            '[]'::jsonb
          ) FROM due); 
        END;
        $$;
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(_SQL)


def downgrade() -> None:
    op.execute(_SQL.replace("PARTITION BY deal_id,kind", "PARTITION BY deal_id"))

