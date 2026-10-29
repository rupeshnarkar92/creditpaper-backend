"""Claim ingestion work fairly across deals.

Revision ID: US_claim_deal_fairness
Revises: US_archive_match_serial_claim
"""

from alembic import op

revision = "US_claim_deal_fairness"
down_revision = "US_archive_match_serial_claim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.claim_processing_jobs(
          p_tenant uuid,p_limit integer,p_kinds text[] DEFAULT NULL
        ) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE claimed jsonb; effective_limit integer;
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          PERFORM pg_advisory_xact_lock(hashtextextended('queue:'||p_tenant::text,0));
          PERFORM public.sweep_processing_job_expired_leases(p_tenant);
          SELECT GREATEST(0,LEAST(GREATEST(p_limit,1),16)-count(*))::integer
          INTO effective_limit
          FROM public.processing_jobs active
          WHERE active.tenant_id=p_tenant AND active.status='running'
            AND active.lease_until>clock_timestamp()
            AND (p_kinds IS NULL OR active.kind=ANY(p_kinds));

          WITH eligible AS MATERIALIZED (
            SELECT j.id,j.deal_id,j.created_at,
              row_number() OVER (
                PARTITION BY j.deal_id
                ORDER BY CASE j.kind WHEN 'archive_import' THEN 0 WHEN 'parse' THEN 10
                                     WHEN 'parse_ocr' THEN 20 ELSE 30 END,
                         j.created_at,j.id
              ) AS deal_position
            FROM public.processing_jobs j
            WHERE j.tenant_id=p_tenant AND j.status='pending'
              AND j.available_at<=clock_timestamp() AND j.attempts<3
              AND (p_kinds IS NULL OR j.kind=ANY(p_kinds))
              AND (j.kind<>'archive_match' OR NOT EXISTS(
                SELECT 1 FROM public.processing_jobs other
                WHERE other.tenant_id=j.tenant_id AND other.deal_id=j.deal_id
                  AND other.kind IN('parse','parse_ocr','archive_import')
                  AND other.status IN('pending','running')))
              AND (j.kind NOT IN(
                    'generation','document','extraction','corpus_extraction',
                    'ingest_window','ingest_speculate','ingest_propose','archive_match'
                  ) OR NOT EXISTS(
                    SELECT 1 FROM public.processing_jobs busy
                    WHERE busy.tenant_id=j.tenant_id AND busy.deal_id=j.deal_id
                      AND busy.id<>j.id AND busy.status='running'
                      AND busy.lease_until>clock_timestamp()
                      AND busy.kind IN(
                        'generation','document','extraction','corpus_extraction',
                        'ingest_window','ingest_speculate','ingest_propose','archive_match'
                      )))
              AND (j.kind<>'generation' OR NOT EXISTS(
                SELECT 1 FROM public.processing_jobs g
                WHERE g.tenant_id=j.tenant_id AND g.kind='generation' AND g.id<>j.id
                  AND g.status='running' AND g.lease_until>clock_timestamp()))
          ), candidates AS MATERIALIZED (
            SELECT j.id,j.created_at
            FROM public.processing_jobs j
            JOIN eligible e ON e.id=j.id
            ORDER BY e.deal_position,e.created_at,e.deal_id,e.id
            FOR UPDATE OF j SKIP LOCKED LIMIT effective_limit
          ), updated AS (
            UPDATE public.processing_jobs j
            SET status='running',attempts=j.attempts+1,lease_token=gen_random_uuid(),
                lease_until=clock_timestamp()+interval '90 seconds',updated_at=clock_timestamp()
            FROM candidates c WHERE j.tenant_id=p_tenant AND j.id=c.id
            RETURNING j.id,j.deal_id,j.kind,j.payload,j.attempts,j.lease_token,
              GREATEST(0,floor(EXTRACT(epoch FROM (clock_timestamp()-c.created_at))*1000))::bigint
                AS queue_wait_ms
          )
          SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'id',id,'deal_id',deal_id,'kind',kind,'payload',payload,'attempts',attempts,
            'lease',lease_token,'queue_wait_ms',queue_wait_ms
          )),'[]'::jsonb) INTO claimed FROM updated;
          RETURN claimed;
        END $$;
        COMMENT ON FUNCTION public.claim_processing_jobs(uuid,integer,text[])
          IS 'Tenant/lane capacity -> fenced leases round-robin across deals to prevent large uploads starving newer deals.';
        REVOKE ALL ON FUNCTION public.claim_processing_jobs(uuid,integer,text[])
          FROM PUBLIC,anon,authenticated;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.claim_processing_jobs(uuid,integer,text[])
              TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Forward-only in deployed environments. Replaying US_claim_lane_capacity
    # restores the previous age-ordered definition for local rollback testing.
    op.execute("DROP FUNCTION IF EXISTS public.claim_processing_jobs(uuid,integer,text[])")
