"""Keep speculative model work behind all archive matching.

Revision ID: US_archive_model_order
Revises: US_archive_resume_reconcile
"""

from alembic import op

revision = "US_archive_model_order"
down_revision = "US_archive_resume_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION public.archive_pipeline_ready(p_tenant uuid,p_job uuid)
        RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
          SELECT CASE WHEN candidate.kind<>'ingest_speculate' THEN true ELSE NOT EXISTS(
            SELECT 1 FROM public.processing_jobs matcher
            WHERE matcher.tenant_id=candidate.tenant_id
              AND matcher.deal_id=candidate.deal_id
              AND matcher.kind='archive_match'
              AND matcher.status IN('pending','running')
          ) END
          FROM public.processing_jobs candidate
          WHERE candidate.tenant_id=p_tenant AND candidate.id=p_job
            AND p_tenant=NULLIF(current_setting('app.tenant_id',true),'')::uuid
        $$;

        CREATE OR REPLACE FUNCTION public.claim_processing_job_for_broker(
          p_tenant uuid,p_job uuid,p_task_id text,p_lease_seconds integer DEFAULT 90
        ) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE j public.processing_jobs%ROWTYPE; new_lease uuid;
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          PERFORM pg_advisory_xact_lock(hashtextextended('queue:' || p_tenant::text,0));
          PERFORM public.sweep_processing_job_expired_leases(p_tenant);
          SELECT * INTO j FROM public.processing_jobs
          WHERE tenant_id=p_tenant AND id=p_job FOR UPDATE;
          IF NOT FOUND OR j.status IN('complete','failed') THEN RETURN NULL; END IF;
          IF j.status='pending' AND (j.available_at>clock_timestamp() OR j.attempts>=3) THEN RETURN NULL; END IF;
          IF j.status='running' AND j.lease_until>clock_timestamp() THEN RETURN NULL; END IF;
          IF NOT public.archive_match_ready(p_tenant,p_job)
             OR NOT public.archive_pipeline_ready(p_tenant,p_job) THEN RETURN NULL; END IF;
          IF j.kind IN(
              'generation','document','extraction','corpus_extraction',
              'ingest_window','ingest_speculate','ingest_propose','archive_match'
            ) AND EXISTS(
              SELECT 1 FROM public.processing_jobs busy
              WHERE busy.tenant_id=j.tenant_id AND busy.deal_id=j.deal_id AND busy.id<>j.id
                AND busy.status='running' AND busy.lease_until>clock_timestamp()
                AND busy.kind IN(
                  'generation','document','extraction','corpus_extraction',
                  'ingest_window','ingest_speculate','ingest_propose','archive_match'
                )
          ) THEN RETURN NULL; END IF;
          IF j.kind='generation' AND EXISTS(
            SELECT 1 FROM public.processing_jobs generation
            WHERE generation.tenant_id=j.tenant_id AND generation.id<>j.id
              AND generation.kind='generation' AND generation.status='running'
              AND generation.lease_until>clock_timestamp()
          ) THEN RETURN NULL; END IF;
          new_lease := gen_random_uuid();
          UPDATE public.processing_jobs SET status='running',attempts=attempts+1,
            lease_token=new_lease,
            lease_until=clock_timestamp()+make_interval(secs=>GREATEST(30,LEAST(p_lease_seconds,300))),
            queue_backend='celery',broker_task_id=p_task_id,updated_at=clock_timestamp()
          WHERE tenant_id=p_tenant AND id=p_job;
          RETURN jsonb_build_object(
            'id',j.id,'deal_id',j.deal_id,'kind',j.kind,'payload',j.payload,
            'attempts',j.attempts+1,'lease',new_lease,
            'queue_wait_ms',GREATEST(0,floor(EXTRACT(epoch FROM(clock_timestamp()-j.created_at))*1000))::bigint
          );
        END $$;

        CREATE OR REPLACE FUNCTION public.pending_processing_jobs_for_delivery(
          p_tenant uuid,p_kinds text[],p_limit integer DEFAULT 25
        ) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          PERFORM public.sweep_processing_job_expired_leases(p_tenant);
          RETURN (
            WITH ranked AS (
              SELECT id,kind,deal_id,created_at,
                row_number() OVER(PARTITION BY deal_id,kind ORDER BY created_at,id) AS deal_rank
              FROM public.processing_jobs
              WHERE tenant_id=p_tenant AND attempts<3
                AND (p_kinds IS NULL OR kind=ANY(p_kinds))
                AND ((status='pending' AND available_at<=clock_timestamp())
                     OR (status='running' AND lease_until<=clock_timestamp()))
                AND public.archive_match_ready(p_tenant,id)
                AND public.archive_pipeline_ready(p_tenant,id)
            ), due AS (
              SELECT id,kind,created_at,deal_rank FROM ranked
              ORDER BY deal_rank,created_at,id LIMIT GREATEST(1,LEAST(p_limit,250))
            )
            SELECT COALESCE(jsonb_agg(
              jsonb_build_object('id',id,'kind',kind) ORDER BY deal_rank,created_at,id
            ),'[]'::jsonb) FROM due
          );
        END $$;

        REVOKE ALL ON FUNCTION public.archive_pipeline_ready(uuid,uuid) FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.archive_pipeline_ready(uuid,uuid) TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    raise RuntimeError("Retain archive-before-model ordering")
