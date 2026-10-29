"""Recover expired broker claims after worker crashes.

Revision ID: US_celery_crash_recovery
Revises: US_resume_deal_processing
"""

from alembic import op

revision = "US_celery_crash_recovery"
down_revision = "US_resume_deal_processing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
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

        CREATE OR REPLACE FUNCTION public.pending_deal_jobs_for_delivery(
          p_tenant uuid,p_deal text,p_kinds text[],p_limit integer DEFAULT 1000
        ) RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
          SELECT COALESCE(jsonb_agg(jsonb_build_object('id',id,'kind',kind) ORDER BY created_at),'[]')
          FROM (
            SELECT id,kind,created_at FROM public.processing_jobs
            WHERE tenant_id=p_tenant AND deal_id=p_deal AND attempts<3
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


def downgrade() -> None:
    # The prior revisions recreate these functions with pending-only recovery.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.pending_processing_jobs_for_delivery(
          p_tenant uuid,p_kinds text[],p_limit integer DEFAULT 1000
        ) RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
          SELECT COALESCE(jsonb_agg(jsonb_build_object('id',id,'kind',kind) ORDER BY created_at),'[]')
          FROM (
            SELECT id,kind,created_at FROM public.processing_jobs
            WHERE tenant_id=p_tenant AND status='pending' AND available_at<=clock_timestamp()
              AND attempts<3 AND (p_kinds IS NULL OR kind=ANY(p_kinds))
            ORDER BY created_at LIMIT GREATEST(1,LEAST(p_limit,5000))
          ) due
        $$;
        CREATE OR REPLACE FUNCTION public.pending_deal_jobs_for_delivery(
          p_tenant uuid,p_deal text,p_kinds text[],p_limit integer DEFAULT 1000
        ) RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
          SELECT COALESCE(jsonb_agg(jsonb_build_object('id',id,'kind',kind) ORDER BY created_at),'[]')
          FROM (
            SELECT id,kind,created_at FROM public.processing_jobs
            WHERE tenant_id=p_tenant AND deal_id=p_deal AND status='pending'
              AND available_at<=clock_timestamp() AND attempts<3
              AND (p_kinds IS NULL OR kind=ANY(p_kinds))
            ORDER BY created_at LIMIT GREATEST(1,LEAST(p_limit,5000))
          ) due
        $$;
        """
    )
