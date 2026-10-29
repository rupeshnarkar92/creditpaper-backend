"""Terminalize retry-exhausted jobs that can no longer be claimed.

Revision ID: US_terminalize_exhausted_jobs
Revises: US_ocr_failure_visibility
"""

from alembic import op

revision = "US_terminalize_exhausted_jobs"
down_revision = "US_ocr_failure_visibility"
branch_labels = None
depends_on = None


def _sweep_function(*, include_stranded: bool) -> str:
    stranded = "OR (j.status='pending' AND j.attempts>=3)" if include_stranded else ""
    return f"""
    CREATE OR REPLACE FUNCTION public.sweep_processing_job_expired_leases(p_tenant_id uuid)
    RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY INVOKER
    SET search_path=pg_catalog,public AS $$
    DECLARE moved_count integer;
    BEGIN
      WITH expired AS (
        UPDATE public.processing_jobs j
        SET status=CASE WHEN j.attempts>=3 THEN 'failed' ELSE 'pending' END,
            result=CASE WHEN j.attempts>=3
              THEN jsonb_build_object('error','Processing retry limit exhausted')
              ELSE jsonb_build_object('error','Worker lease expired; retry scheduled') END,
            last_error_type=CASE WHEN j.attempts>=3 THEN 'RetryLimitExhausted' ELSE 'LeaseExpired' END,
            available_at=clock_timestamp(),lease_until=NULL,lease_token=NULL,
            updated_at=clock_timestamp()
        WHERE j.tenant_id=p_tenant_id AND (
          (j.status='running' AND j.lease_until<clock_timestamp()) {stranded}
        )
        RETURNING j.tenant_id,j.id,j.deal_id,j.kind,j.resource_id,j.attempts,
                  j.last_error_type,j.result,j.status
      ), inserted AS (
        INSERT INTO public.processing_job_dead_letters
          (tenant_id,job_id,deal_id,kind,resource_id,attempts,error_type,result)
        SELECT tenant_id,id,deal_id,kind,resource_id,attempts,
               COALESCE(last_error_type,'RetryLimitExhausted'),result
        FROM expired WHERE status='failed'
        ON CONFLICT (tenant_id,job_id) DO NOTHING
        RETURNING 1
      )
      SELECT count(*) INTO moved_count FROM expired WHERE status='failed';
      RETURN moved_count;
    END $$;
    """


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(_sweep_function(include_stranded=True))
    op.execute(
        """
        WITH stranded AS (
          UPDATE public.processing_jobs j
          SET status='failed',
              result=jsonb_build_object('error','Processing retry limit exhausted'),
              last_error_type='RetryLimitExhausted',available_at=clock_timestamp(),
              lease_until=NULL,lease_token=NULL,updated_at=clock_timestamp()
          WHERE j.status='pending' AND j.attempts>=3
          RETURNING j.tenant_id,j.id,j.deal_id,j.kind,j.resource_id,j.attempts,
                    j.last_error_type,j.result
        )
        INSERT INTO public.processing_job_dead_letters
          (tenant_id,job_id,deal_id,kind,resource_id,attempts,error_type,result)
        SELECT tenant_id,id,deal_id,kind,resource_id,attempts,last_error_type,result
        FROM stranded ON CONFLICT (tenant_id,job_id) DO NOTHING
        """
    )
    op.execute(
        "COMMENT ON FUNCTION public.sweep_processing_job_expired_leases(uuid) IS "
        "'Tenant lease reaper; retries live work and terminalizes expired or stranded retry-exhausted jobs.'"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.sweep_processing_job_expired_leases(uuid) "
        "FROM PUBLIC,anon,authenticated"
    )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.sweep_processing_job_expired_leases(uuid)
              TO creditpaper_runtime;
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute(_sweep_function(include_stranded=False))
