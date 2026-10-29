"""Finish several independently fenced jobs in one tenant transaction.

Revision ID: US_job_batch_finish
Revises: US_claim_prepared_docs
"""

from alembic import op

revision = "US_job_batch_finish"
down_revision = "US_claim_prepared_docs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION public.finish_processing_jobs_observed_batch(
          p_tenant uuid,p_items jsonb
        ) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE item jsonb; final_status text; output jsonb := '[]'::jsonb;
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          IF jsonb_typeof(p_items)<>'array' OR jsonb_array_length(p_items) NOT BETWEEN 1 AND 16 THEN
            RAISE EXCEPTION 'job finish batch must contain 1 to 16 jobs' USING ERRCODE='22023';
          END IF;
          FOR item IN SELECT value FROM jsonb_array_elements(p_items) LOOP
            final_status := public.finish_processing_job_observed(
              p_tenant,(item->>'id')::uuid,(item->>'lease')::uuid,item->>'outcome',
              COALESCE(item->'result','{}'::jsonb),item->>'error_type',
              COALESCE((item->>'retry_delay')::integer,0),
              COALESCE(item->'stage_timings','{}'::jsonb)
            );
            output := output || jsonb_build_array(jsonb_build_object(
              'id',item->>'id','status',final_status
            ));
          END LOOP;
          RETURN output;
        END $$;
        COMMENT ON FUNCTION public.finish_processing_jobs_observed_batch(uuid,jsonb)
          IS 'Tenant and 1-16 fenced job outcomes with telemetry -> per-job durable statuses in one transaction.';
        REVOKE ALL ON FUNCTION public.finish_processing_jobs_observed_batch(uuid,jsonb)
          FROM PUBLIC,anon,authenticated;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.finish_processing_jobs_observed_batch(uuid,jsonb)
              TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.finish_processing_jobs_observed_batch(uuid,jsonb)")
