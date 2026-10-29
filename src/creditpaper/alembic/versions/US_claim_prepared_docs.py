"""Prepare claimed document metadata/cache inside the batch-claim transaction.

Revision ID: US_claim_prepared_docs
Revises: US_job_batch_claim
"""

from alembic import op

revision = "US_claim_prepared_docs"
down_revision = "US_job_batch_claim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION public.claim_processing_jobs_prepared(
          p_tenant uuid,p_limit integer,p_kinds text[],p_parser_version text
        ) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE claimed jsonb; item jsonb; prepared jsonb; output jsonb := '[]'::jsonb;
        BEGIN
          claimed := public.claim_processing_jobs(p_tenant,p_limit,p_kinds);
          FOR item IN SELECT value FROM jsonb_array_elements(claimed) LOOP
            prepared := NULL;
            IF item->>'kind' IN ('parse','parse_ocr','document')
               AND item->'payload'->>'version_id' IS NOT NULL THEN
              prepared := public.begin_document_processing_cached(
                p_tenant,item->>'deal_id',(item->'payload'->>'version_id')::uuid,p_parser_version
              );
            END IF;
            output := output || jsonb_build_array(
              item || jsonb_build_object('processing_input',prepared)
            );
          END LOOP;
          RETURN output;
        END $$;
        COMMENT ON FUNCTION public.claim_processing_jobs_prepared(uuid,integer,text[],text)
          IS 'Tenant/count/kinds/parser -> fenced jobs with document metadata and extraction-cache hits prepared in the same transaction.';
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
