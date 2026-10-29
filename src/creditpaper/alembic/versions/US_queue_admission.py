"""Add bounded tenant/deal queue admission and claim support.

Revision ID: US_queue_admission
Revises: US_progressive_ready
"""

from alembic import op

revision = "US_queue_admission"
down_revision = "US_progressive_ready"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION public.processing_queue_admission(
          p_tenant uuid,p_deal text,p_requested integer DEFAULT 1,
          p_tenant_limit integer DEFAULT 2000,p_deal_limit integer DEFAULT 1200
        ) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE tenant_active integer; deal_active integer;
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          IF p_requested<1 OR p_tenant_limit<1 OR p_deal_limit<1 THEN
            RAISE EXCEPTION 'Queue limits must be positive' USING ERRCODE='22023';
          END IF;
          SELECT count(*)::integer,
                 count(*) FILTER(WHERE deal_id=p_deal)::integer
            INTO tenant_active,deal_active
          FROM public.processing_jobs
          WHERE tenant_id=p_tenant AND status IN('pending','running');
          RETURN jsonb_build_object(
            'accepted',tenant_active+p_requested<=p_tenant_limit
                       AND deal_active+p_requested<=p_deal_limit,
            'tenant_active',tenant_active,'deal_active',deal_active,
            'tenant_limit',p_tenant_limit,'deal_limit',p_deal_limit,
            'requested',p_requested
          );
        END $$;
        COMMENT ON FUNCTION public.processing_queue_admission(uuid,text,integer,integer,integer)
          IS 'Tenant/deal/requested counts and limits -> admission decision for bounded ingestion backpressure.';
        REVOKE ALL ON FUNCTION public.processing_queue_admission(uuid,text,integer,integer,integer)
          FROM PUBLIC,anon,authenticated;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.processing_queue_admission(uuid,text,integer,integer,integer)
              TO creditpaper_runtime;
          END IF;
        END $$;

        CREATE INDEX IF NOT EXISTS ix_processing_tenant_deal_active
          ON public.processing_jobs(tenant_id,deal_id,created_at)
          WHERE status IN('pending','running');
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_processing_tenant_deal_active")
    op.execute("DROP FUNCTION IF EXISTS public.processing_queue_admission(uuid,text,integer,integer,integer)")
