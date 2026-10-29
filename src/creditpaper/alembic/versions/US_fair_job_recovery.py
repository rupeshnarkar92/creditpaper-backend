"""Bound recovery fairly across deals within each tenant.

Revision ID: US_fair_job_recovery
Revises: US_celery_crash_recovery
"""

from alembic import op

revision = "US_fair_job_recovery"
down_revision = "US_celery_crash_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.pending_processing_jobs_for_delivery(
          p_tenant uuid,p_kinds text[],p_limit integer DEFAULT 25
        ) RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
          WITH ranked AS (
            SELECT id,kind,deal_id,created_at,
                   row_number() OVER (PARTITION BY deal_id ORDER BY created_at,id) AS deal_rank
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
          ) FROM due
        $$;
        COMMENT ON FUNCTION public.pending_processing_jobs_for_delivery(uuid,text[],integer)
          IS 'Bounded round-robin recovery across deals; PostgreSQL remains durable source of truth';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.pending_processing_jobs_for_delivery(
          p_tenant uuid,p_kinds text[],p_limit integer DEFAULT 1000
        ) RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
          SELECT COALESCE(jsonb_agg(jsonb_build_object('id',id,'kind',kind) ORDER BY created_at),'[]')
          FROM (
            SELECT id,kind,created_at FROM public.processing_jobs
            WHERE tenant_id=p_tenant AND attempts<3
              AND (p_kinds IS NULL OR kind=ANY(p_kinds))
              AND (
                (status='pending' AND available_at<=clock_timestamp())
                OR (status='running' AND lease_until<=clock_timestamp())
              )
            ORDER BY created_at LIMIT GREATEST(1,LEAST(p_limit,5000))
          ) due
        $$;
        """
    )
