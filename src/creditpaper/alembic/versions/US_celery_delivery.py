"""Add event-driven broker delivery without making Redis authoritative.

Revision ID: US_celery_delivery
Revises: US_claim_deal_fairness
"""

from alembic import op

revision = "US_celery_delivery"
down_revision = "US_claim_deal_fairness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE public.processing_jobs
          ADD COLUMN IF NOT EXISTS queue_backend text NOT NULL DEFAULT 'database',
          ADD COLUMN IF NOT EXISTS broker_task_id text;
        CREATE INDEX IF NOT EXISTS ix_processing_broker_recovery
          ON public.processing_jobs(queue_backend,status,available_at,created_at);

        CREATE OR REPLACE FUNCTION public.claim_processing_job_for_broker(
          p_tenant uuid,p_job uuid,p_task_id text,p_lease_seconds integer DEFAULT 4200
        ) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE j public.processing_jobs%ROWTYPE; new_lease uuid;
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          SELECT * INTO j FROM public.processing_jobs
          WHERE tenant_id=p_tenant AND id=p_job FOR UPDATE;
          IF NOT FOUND OR j.status IN('complete','failed') THEN RETURN NULL; END IF;
          IF j.status='pending' AND (j.available_at>clock_timestamp() OR j.attempts>=3) THEN RETURN NULL; END IF;
          IF j.status='running' AND j.broker_task_id IS DISTINCT FROM p_task_id
             AND j.lease_until>clock_timestamp() THEN RETURN NULL; END IF;
          IF j.kind='archive_match' AND EXISTS(
            SELECT 1 FROM public.processing_jobs blocker
            WHERE blocker.tenant_id=j.tenant_id AND blocker.deal_id=j.deal_id
              AND blocker.kind IN('parse','parse_ocr','archive_import')
              AND blocker.status IN('pending','running')
          ) THEN RETURN NULL; END IF;
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
          UPDATE public.processing_jobs SET
            status='running',
            attempts=CASE WHEN j.status='pending' THEN attempts+1 ELSE attempts END,
            lease_token=new_lease,
            lease_until=clock_timestamp()+make_interval(secs=>GREATEST(300,LEAST(p_lease_seconds,7200))),
            queue_backend='celery',broker_task_id=p_task_id,updated_at=clock_timestamp()
          WHERE tenant_id=p_tenant AND id=p_job;
          RETURN jsonb_build_object(
            'id',j.id,'deal_id',j.deal_id,'kind',j.kind,'payload',j.payload,
            'attempts',CASE WHEN j.status='pending' THEN j.attempts+1 ELSE j.attempts END,
            'lease',new_lease,
            'queue_wait_ms',GREATEST(0,floor(EXTRACT(epoch FROM(clock_timestamp()-j.created_at))*1000))::bigint
          );
        END $$;

        CREATE OR REPLACE FUNCTION public.processing_job_delivery_status(p_tenant uuid,p_job uuid)
        RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
          SELECT jsonb_build_object(
            'status',status,
            'retry_in_seconds',GREATEST(0,EXTRACT(epoch FROM(available_at-clock_timestamp())))
          ) FROM public.processing_jobs WHERE tenant_id=p_tenant AND id=p_job
        $$;

        CREATE OR REPLACE FUNCTION public.processing_job_kind(p_tenant uuid,p_job uuid)
        RETURNS text LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
          SELECT kind FROM public.processing_jobs WHERE tenant_id=p_tenant AND id=p_job
        $$;

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

        COMMENT ON FUNCTION public.claim_processing_job_for_broker(uuid,uuid,text,integer) IS
          'Atomically claims p_job for p_tenant/p_task_id for p_lease_seconds and returns the fenced job payload; NULL means terminal, not due, or blocked.';
        COMMENT ON FUNCTION public.processing_job_delivery_status(uuid,uuid) IS
          'Returns durable status and retry delay for p_job owned by p_tenant after ephemeral broker execution.';
        COMMENT ON FUNCTION public.processing_job_kind(uuid,uuid) IS
          'Returns the queue-routing kind for p_job owned by p_tenant, or NULL when it is not visible.';
        COMMENT ON FUNCTION public.pending_processing_jobs_for_delivery(uuid,text[],integer) IS
          'Returns up to p_limit due durable job ids/kinds for p_tenant and optional p_kinds; used only to recover broker delivery.';
        REVOKE ALL ON FUNCTION public.claim_processing_job_for_broker(uuid,uuid,text,integer),
          public.processing_job_delivery_status(uuid,uuid),
          public.processing_job_kind(uuid,uuid),
          public.pending_processing_jobs_for_delivery(uuid,text[],integer) FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
            REVOKE ALL ON FUNCTION public.claim_processing_job_for_broker(uuid,uuid,text,integer),
              public.processing_job_delivery_status(uuid,uuid),public.processing_job_kind(uuid,uuid),
              public.pending_processing_jobs_for_delivery(uuid,text[],integer) FROM anon;
          END IF;
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
            REVOKE ALL ON FUNCTION public.claim_processing_job_for_broker(uuid,uuid,text,integer),
              public.processing_job_delivery_status(uuid,uuid),public.processing_job_kind(uuid,uuid),
              public.pending_processing_jobs_for_delivery(uuid,text[],integer) FROM authenticated;
          END IF;
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.claim_processing_job_for_broker(uuid,uuid,text,integer),
              public.processing_job_delivery_status(uuid,uuid),public.processing_job_kind(uuid,uuid),
              public.pending_processing_jobs_for_delivery(uuid,text[],integer) TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.pending_processing_jobs_for_delivery(uuid,text[],integer)")
    op.execute("DROP FUNCTION IF EXISTS public.processing_job_kind(uuid,uuid)")
    op.execute("DROP FUNCTION IF EXISTS public.processing_job_delivery_status(uuid,uuid)")
    op.execute("DROP FUNCTION IF EXISTS public.claim_processing_job_for_broker(uuid,uuid,text,integer)")
    op.execute("DROP INDEX IF EXISTS public.ix_processing_broker_recovery")
    op.execute("ALTER TABLE public.processing_jobs DROP COLUMN IF EXISTS broker_task_id")
    op.execute("ALTER TABLE public.processing_jobs DROP COLUMN IF EXISTS queue_backend")
