"""Finish a job and persist its aggregate stage timings in one transaction.

Revision ID: US_job_finish_observed
Revises: US_queue_admission
"""

from alembic import op

revision = "US_job_finish_observed"
down_revision = "US_queue_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION public.finish_processing_job_observed(
          p_tenant uuid,p_job uuid,p_lease uuid,p_outcome text,p_result jsonb,
          p_error_type text,p_retry_delay integer,p_stage_timings jsonb
        ) RETURNS text LANGUAGE plpgsql VOLATILE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE final_status text;
        BEGIN
          final_status := public.finish_processing_job(
            p_tenant,p_job,p_lease,p_outcome,p_result,p_error_type,p_retry_delay
          );
          IF final_status IS NOT NULL THEN
            UPDATE public.processing_jobs
            SET stage_timings=COALESCE(stage_timings,'{}'::jsonb)
                              || COALESCE(p_stage_timings,'{}'::jsonb)
            WHERE tenant_id=p_tenant AND id=p_job;
          END IF;
          RETURN final_status;
        END $$;
        COMMENT ON FUNCTION public.finish_processing_job_observed(uuid,uuid,uuid,text,jsonb,text,integer,jsonb)
          IS 'Fenced job completion inputs plus operational stage timings -> durable status; completes and records telemetry atomically.';
        REVOKE ALL ON FUNCTION public.finish_processing_job_observed(uuid,uuid,uuid,text,jsonb,text,integer,jsonb)
          FROM PUBLIC,anon,authenticated;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.finish_processing_job_observed(uuid,uuid,uuid,text,jsonb,text,integer,jsonb)
              TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.finish_processing_job_observed(uuid,uuid,uuid,text,jsonb,text,integer,jsonb)"
    )
