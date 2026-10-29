"""Expose tenant/deal-scoped durable jobs for safe broker redelivery.

Revision ID: US_resume_deal_processing
Revises: US_celery_delivery
"""

from alembic import op

revision = "US_resume_deal_processing"
down_revision = "US_celery_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.pending_deal_jobs_for_delivery(
          p_tenant uuid,p_deal text,p_kinds text[],p_limit integer DEFAULT 1000
        ) RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
          SELECT COALESCE(jsonb_agg(jsonb_build_object('id',id,'kind',kind)
                                    ORDER BY created_at),'[]'::jsonb)
          FROM (
            SELECT id,kind,created_at FROM public.processing_jobs
            WHERE tenant_id=p_tenant AND deal_id=p_deal AND status='pending'
              AND available_at<=clock_timestamp() AND attempts<3
              AND (p_kinds IS NULL OR kind=ANY(p_kinds))
            ORDER BY created_at LIMIT GREATEST(1,LEAST(p_limit,5000))
          ) due
        $$;
        COMMENT ON FUNCTION public.pending_deal_jobs_for_delivery(uuid,text,text[],integer) IS
          'Tenant/deal -> due durable job ids and kinds for idempotent broker redelivery.';
        REVOKE ALL ON FUNCTION public.pending_deal_jobs_for_delivery(uuid,text,text[],integer)
          FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
            REVOKE ALL ON FUNCTION public.pending_deal_jobs_for_delivery(uuid,text,text[],integer)
              FROM anon;
          END IF;
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
            REVOKE ALL ON FUNCTION public.pending_deal_jobs_for_delivery(uuid,text,text[],integer)
              FROM authenticated;
          END IF;
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.pending_deal_jobs_for_delivery(uuid,text,text[],integer)
              TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.pending_deal_jobs_for_delivery(uuid,text,text[],integer)"
    )
