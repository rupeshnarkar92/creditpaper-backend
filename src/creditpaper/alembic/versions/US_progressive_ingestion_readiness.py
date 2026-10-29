"""Expose separate fact and generation readiness gates.

Revision ID: US_progressive_ready
Revises: US_extraction_ocr_lane
"""

from alembic import op

revision = "US_progressive_ready"
down_revision = "US_extraction_ocr_lane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER FUNCTION public.deal_ingestion_status(uuid,text)
          RENAME TO deal_ingestion_status_pre_progressive;

        CREATE FUNCTION public.deal_ingestion_status(p_tenant uuid,p_deal text)
        RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
          WITH base AS (
            SELECT public.deal_ingestion_status_pre_progressive(p_tenant,p_deal) AS value
          ), health AS (
            SELECT COALESCE(public.processing_health(p_tenant,p_deal),'{}'::jsonb) AS value
          ), state AS (
            SELECT base.value AS base,
              COALESCE((health.value->>'ready')::integer,0) AS ready,
              COALESCE((health.value->>'failed')::integer,0) AS failed,
              COALESCE((health.value->>'queued')::integer,0) AS queued,
              COALESCE((health.value->>'processing')::integer,0) AS processing
            FROM base CROSS JOIN health
          )
          SELECT base || jsonb_build_object(
            'ready',ready,
            'failed',GREATEST(COALESCE((base->>'failed')::integer,0),failed),
            'queued',queued,
            'processing',processing,
            'facts_locked',ready=0,
            'generation_locked',
              ready=0 OR COALESCE((base->>'uploading')::integer,0)>0
                      OR queued>0 OR processing>0,
            'locked',
              ready=0 OR COALESCE((base->>'uploading')::integer,0)>0
                      OR queued>0 OR processing>0
          ) FROM state
        $$;

        COMMENT ON FUNCTION public.deal_ingestion_status(uuid,text) IS
          'Tenant/deal readiness: facts unlock after the first ready document; generation unlocks after all active work is terminal. Failed documents remain warnings.';
        REVOKE ALL ON FUNCTION public.deal_ingestion_status(uuid,text) FROM PUBLIC,anon,authenticated;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.deal_ingestion_status(uuid,text)
              TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION public.deal_ingestion_status(uuid,text);
        ALTER FUNCTION public.deal_ingestion_status_pre_progressive(uuid,text)
          RENAME TO deal_ingestion_status;
        """
    )
