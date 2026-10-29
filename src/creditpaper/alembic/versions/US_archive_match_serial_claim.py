"""Claim only one eligible archive matcher for a deal at a time.

Revision ID: US_archive_match_serial_claim
Revises: US_server_side_cache_claim
"""

from alembic import op

revision = "US_archive_match_serial_claim"
down_revision = "US_server_side_cache_claim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.claim_processing_jobs_prepared(
          p_tenant uuid,p_limit integer,p_kinds text[],p_parser_version text
        ) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE claimed jsonb; item jsonb; prepared jsonb; output jsonb := '[]'::jsonb;
        DECLARE cache_payload jsonb; claim_limit integer := p_limit;
        BEGIN
          -- Split folder uploads create several match jobs for one deal. Once
          -- parsing is terminal, claim exactly one: each matcher rebuilds the
          -- same board and concurrent matchers only contend on its rows.
          IF (p_kinds IS NULL OR 'archive_match'=ANY(p_kinds)) AND EXISTS(
            SELECT 1 FROM public.processing_jobs j
            WHERE j.tenant_id=p_tenant AND j.kind='archive_match' AND j.status='pending'
              AND j.available_at<=clock_timestamp() AND j.attempts<3
              AND NOT EXISTS(
                SELECT 1 FROM public.processing_jobs blockers
                WHERE blockers.tenant_id=j.tenant_id AND blockers.deal_id=j.deal_id
                  AND blockers.kind IN('parse','parse_ocr','archive_import')
                  AND blockers.status IN('pending','running'))
              AND NOT EXISTS(
                SELECT 1 FROM public.processing_jobs matcher
                WHERE matcher.tenant_id=j.tenant_id AND matcher.deal_id=j.deal_id
                  AND matcher.kind='archive_match' AND matcher.status='running'
                  AND matcher.lease_until>clock_timestamp())
          ) THEN
            claim_limit := 1;
          END IF;
          claimed := public.claim_processing_jobs(p_tenant,claim_limit,p_kinds);
          FOR item IN SELECT value FROM jsonb_array_elements(claimed) LOOP
            prepared := NULL;
            IF item->>'kind' IN ('parse','parse_ocr','document')
               AND item->'payload'->>'version_id' IS NOT NULL THEN
              prepared := public.begin_document_processing_cached(
                p_tenant,item->>'deal_id',(item->'payload'->>'version_id')::uuid,p_parser_version
              );
            END IF;
            cache_payload := prepared->'cached_extraction';
            IF item->>'kind' IN ('parse','parse_ocr') AND cache_payload IS NOT NULL
               AND jsonb_typeof(cache_payload)='object' THEN
              IF NOT public.finish_document_processing_cached(
                p_tenant,item->>'deal_id',(item->'payload'->>'version_id')::uuid,
                prepared->>'content_sha256',p_parser_version,cache_payload
              ) THEN
                RAISE EXCEPTION 'cached document version not found' USING ERRCODE='P0002';
              END IF;
              UPDATE public.processing_jobs j
              SET status='complete',result=jsonb_build_object(
                    'status','complete','cache_hit',true,'server_side',true
                  ),
                  stage_timings=jsonb_build_object(
                    'queue_wait',COALESCE((item->>'queue_wait_ms')::bigint,0),
                    'cache_finalize',0
                  ),
                  lease_token=NULL,lease_until=NULL,updated_at=clock_timestamp()
              WHERE j.tenant_id=p_tenant AND j.id=(item->>'id')::uuid
                AND j.lease_token=(item->>'lease')::uuid AND j.status='running';
            ELSE
              output := output || jsonb_build_array(
                item || jsonb_build_object('processing_input',prepared)
              );
            END IF;
          END LOOP;
          RETURN output;
        END $$;
        COMMENT ON FUNCTION public.claim_processing_jobs_prepared(uuid,integer,text[],text)
          IS 'Capacity-aware claim with server cache completion and one deal-exclusive archive matcher at a time.';
        REVOKE ALL ON FUNCTION public.claim_processing_jobs_prepared(uuid,integer,text[],text)
          FROM PUBLIC,anon,authenticated;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.claim_processing_jobs_prepared(uuid,integer,text[],text)
              TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.claim_processing_jobs_prepared(uuid,integer,text[],text)")
