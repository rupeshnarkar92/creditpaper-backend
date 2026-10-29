"""Bounded processing-job retries and tenant-scoped dead-letter history."""

from alembic import op

revision = "nn_0047_job_retry_dead_letters"
down_revision = "0046_paper_workflow"
branch_labels = None
depends_on = None


_SQL = """
ALTER TABLE public.processing_jobs
    ADD COLUMN available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    ADD COLUMN last_error_type TEXT;

CREATE INDEX ix_processing_due_claim
    ON public.processing_jobs (tenant_id, available_at, created_at)
    WHERE status = 'pending';

CREATE OR REPLACE FUNCTION public.processing_job_tenants() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT DISTINCT tenant_id FROM public.processing_jobs
    WHERE (status = 'pending' AND available_at <= clock_timestamp())
       OR (status = 'running' AND lease_until < clock_timestamp())
$$;

CREATE TABLE public.processing_job_dead_letters (
    tenant_id UUID NOT NULL,
    job_id UUID NOT NULL,
    deal_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts >= 1),
    error_type TEXT NOT NULL,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    failed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, job_id),
    FOREIGN KEY (tenant_id, job_id) REFERENCES public.processing_jobs (tenant_id, id),
    FOREIGN KEY (tenant_id, deal_id) REFERENCES public.deals (tenant_id, id)
);
ALTER TABLE public.processing_job_dead_letters ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.processing_job_dead_letters FORCE ROW LEVEL SECURITY;
CREATE POLICY processing_job_dead_letters_tenant ON public.processing_job_dead_letters
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
REVOKE ALL ON public.processing_job_dead_letters FROM PUBLIC;

REVOKE ALL ON FUNCTION public.processing_job_tenants() FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.finish_processing_job(
    p_tenant_id UUID,
    p_job_id UUID,
    p_lease_token UUID,
    p_outcome TEXT,
    p_result JSONB,
    p_error_type TEXT,
    p_retry_delay_seconds INTEGER
) RETURNS TEXT
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE job_row public.processing_jobs%ROWTYPE;
DECLARE terminal_failure BOOLEAN;
DECLARE target_status TEXT;
BEGIN
    IF p_outcome NOT IN ('complete', 'failed')
       OR p_retry_delay_seconds < 0
       OR p_retry_delay_seconds > 30 THEN
        RAISE EXCEPTION 'invalid job completion parameters' USING ERRCODE = '22023';
    END IF;

    SELECT * INTO job_row
    FROM public.processing_jobs
    WHERE tenant_id = p_tenant_id AND id = p_job_id
      AND lease_token = p_lease_token AND status = 'running'
      AND lease_until > clock_timestamp()
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    terminal_failure := p_outcome = 'failed'
        AND (job_row.attempts >= 3 OR p_retry_delay_seconds = 0);
    target_status := CASE
        WHEN p_outcome = 'complete' THEN 'complete'
        WHEN terminal_failure THEN 'failed'
        ELSE 'pending'
    END;

    UPDATE public.processing_jobs
    SET status = target_status,
        result = COALESCE(p_result, '{}'::jsonb),
        last_error_type = CASE WHEN p_outcome = 'failed' THEN left(p_error_type, 120) ELSE NULL END,
        available_at = CASE
            WHEN target_status = 'pending'
                THEN clock_timestamp() + make_interval(secs => p_retry_delay_seconds)
            ELSE clock_timestamp()
        END,
        lease_until = NULL,
        lease_token = NULL,
        updated_at = clock_timestamp()
    WHERE tenant_id = p_tenant_id AND id = p_job_id;

    IF terminal_failure THEN
        INSERT INTO public.processing_job_dead_letters
            (tenant_id, job_id, deal_id, kind, resource_id, attempts, error_type, result)
        VALUES
            (p_tenant_id, p_job_id, job_row.deal_id, job_row.kind, job_row.resource_id,
             job_row.attempts, left(COALESCE(p_error_type, 'ProcessingFailure'), 120),
             COALESCE(p_result, '{}'::jsonb))
        ON CONFLICT (tenant_id, job_id) DO NOTHING;
    END IF;
    RETURN target_status;
END;
$$;

CREATE OR REPLACE FUNCTION public.sweep_processing_job_expired_leases(
    p_tenant_id UUID
) RETURNS INTEGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE moved_count INTEGER;
BEGIN
    WITH expired AS (
        UPDATE public.processing_jobs j
        SET status = CASE WHEN j.attempts >= 3 THEN 'failed' ELSE 'pending' END,
            result = CASE WHEN j.attempts >= 3
                THEN '{"error":"Worker lease expired after retry limit"}'::jsonb
                ELSE '{"error":"Worker lease expired; retry scheduled"}'::jsonb END,
            last_error_type = 'LeaseExpired',
            -- A lease already waited its full term, so reclaim immediately;
            -- the backoff schedule applies to real handler failures instead.
            available_at = clock_timestamp(),
            lease_until = NULL,
            lease_token = NULL,
            updated_at = clock_timestamp()
        WHERE j.tenant_id = p_tenant_id
          AND j.status = 'running'
          AND j.lease_until < clock_timestamp()
        RETURNING j.tenant_id, j.id, j.deal_id, j.kind, j.resource_id, j.attempts,
                  j.last_error_type, j.result, j.status
    ), inserted AS (
        INSERT INTO public.processing_job_dead_letters
            (tenant_id, job_id, deal_id, kind, resource_id, attempts, error_type, result)
        SELECT tenant_id, id, deal_id, kind, resource_id, attempts,
               COALESCE(last_error_type, 'LeaseExpired'),
               jsonb_build_object('error_type', COALESCE(last_error_type, 'LeaseExpired'))
        FROM expired
        WHERE status = 'failed'
        ON CONFLICT (tenant_id, job_id) DO NOTHING
        RETURNING 1
    )
    SELECT count(*) INTO moved_count FROM expired WHERE status = 'failed';
    RETURN moved_count;
END;
$$;

REVOKE ALL ON FUNCTION public.finish_processing_job(UUID, UUID, UUID, TEXT, JSONB, TEXT, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sweep_processing_job_expired_leases(UUID) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON FUNCTION public.finish_processing_job(UUID, UUID, UUID, TEXT, JSONB, TEXT, INTEGER) FROM anon;
        REVOKE ALL ON FUNCTION public.sweep_processing_job_expired_leases(UUID) FROM anon;
        REVOKE ALL ON FUNCTION public.processing_job_tenants() FROM anon;
        REVOKE ALL ON public.processing_job_dead_letters FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON FUNCTION public.finish_processing_job(UUID, UUID, UUID, TEXT, JSONB, TEXT, INTEGER) FROM authenticated;
        REVOKE ALL ON FUNCTION public.sweep_processing_job_expired_leases(UUID) FROM authenticated;
        REVOKE ALL ON FUNCTION public.processing_job_tenants() FROM authenticated;
        REVOKE ALL ON public.processing_job_dead_letters FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
        GRANT EXECUTE ON FUNCTION public.finish_processing_job(UUID, UUID, UUID, TEXT, JSONB, TEXT, INTEGER) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.sweep_processing_job_expired_leases(UUID) TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.processing_job_tenants() TO creditpaper_runtime;
        GRANT SELECT, INSERT ON public.processing_job_dead_letters TO creditpaper_runtime;
    END IF;
END;
$$;
"""


def upgrade() -> None:
    op.execute(_SQL)


def downgrade() -> None:
    raise RuntimeError(
        "Processing failure history is operational audit data; restore a verified backup instead of downgrading"
    )
