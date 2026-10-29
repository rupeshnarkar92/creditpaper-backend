"""Persist fenced processing stage checkpoints and duration telemetry.

Revision ID: US_ingestion_stage_telemetry
Revises: US_ingestion_ocr_review
"""

from alembic import op

revision = "US_ingestion_stage_telemetry"
down_revision = "US_ingestion_ocr_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE public.processing_jobs
          ADD COLUMN current_stage text,
          ADD COLUMN stage_started_at timestamptz,
          ADD COLUMN stage_timings jsonb NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb;

        CREATE OR REPLACE FUNCTION public.checkpoint_processing_job_stage(
          p_tenant uuid,
          p_job uuid,
          p_lease uuid,
          p_stage text,
          p_state text,
          p_elapsed_ms integer DEFAULT NULL,
          p_metadata jsonb DEFAULT '{}'::jsonb
        ) RETURNS boolean
        LANGUAGE plpgsql VOLATILE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE changed integer;
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          IF p_stage IS NULL OR p_stage !~ '^[a-z][a-z0-9_]{0,63}$' THEN
            RAISE EXCEPTION 'Invalid processing stage' USING ERRCODE='22023';
          END IF;
          IF p_state NOT IN ('running','complete','failed') THEN
            RAISE EXCEPTION 'Invalid checkpoint state' USING ERRCODE='22023';
          END IF;

          UPDATE public.processing_jobs
          SET current_stage=p_stage,
              stage_started_at=CASE WHEN p_state='running' THEN clock_timestamp()
                                    ELSE stage_started_at END,
              stage_timings=(CASE WHEN p_elapsed_ms IS NULL THEN stage_timings ELSE
                jsonb_set(stage_timings, ARRAY[p_stage], to_jsonb(GREATEST(p_elapsed_ms,0)), true) END)
                || COALESCE(p_metadata->'stage_timings','{}'::jsonb),
              checkpoint=jsonb_build_object(
                'stage',p_stage,'state',p_state,'at',clock_timestamp(),'metadata',COALESCE(p_metadata,'{}'::jsonb)
              ),
              updated_at=clock_timestamp()
          WHERE tenant_id=p_tenant AND id=p_job AND lease_token=p_lease
            AND status='running' AND lease_until>clock_timestamp();
          GET DIAGNOSTICS changed = ROW_COUNT;
          RETURN changed=1;
        END $$;

        COMMENT ON FUNCTION public.checkpoint_processing_job_stage(uuid,uuid,uuid,text,text,integer,jsonb)
          IS 'Fenced job lease -> durable current stage, checkpoint metadata, and elapsed milliseconds.';

        CREATE OR REPLACE FUNCTION public.processing_stage_health(p_tenant uuid,p_deal text)
        RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
          SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'kind',kind,'stage',current_stage,'count',n,'oldest_started_at',oldest_started_at
          ) ORDER BY kind,current_stage),'[]'::jsonb)
          FROM (
            SELECT kind,current_stage,count(*)::integer AS n,min(stage_started_at) AS oldest_started_at
            FROM public.processing_jobs
            WHERE tenant_id=p_tenant AND deal_id=p_deal AND status='running'
              AND current_stage IS NOT NULL
              AND p_tenant=NULLIF(current_setting('app.tenant_id',true),'')::uuid
            GROUP BY kind,current_stage
          ) grouped
        $$;
        COMMENT ON FUNCTION public.processing_stage_health(uuid,text)
          IS 'Tenant/deal -> aggregate active job stages without document content.';

        REVOKE ALL ON FUNCTION public.checkpoint_processing_job_stage(
          uuid,uuid,uuid,text,text,integer,jsonb
        ) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.processing_stage_health(uuid,text) FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.checkpoint_processing_job_stage(
              uuid,uuid,uuid,text,text,integer,jsonb
            ) TO creditpaper_runtime;
            GRANT EXECUTE ON FUNCTION public.processing_stage_health(uuid,text)
              TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS public.checkpoint_processing_job_stage(
          uuid,uuid,uuid,text,text,integer,jsonb
        );
        DROP FUNCTION IF EXISTS public.processing_stage_health(uuid,text);
        ALTER TABLE public.processing_jobs
          DROP COLUMN IF EXISTS checkpoint,
          DROP COLUMN IF EXISTS stage_timings,
          DROP COLUMN IF EXISTS stage_started_at,
          DROP COLUMN IF EXISTS current_stage;
        """
    )
