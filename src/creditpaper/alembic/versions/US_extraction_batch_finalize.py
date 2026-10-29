"""Finalize several parsed documents in one tenant transaction.

Revision ID: US_extraction_batch_finalize
Revises: US_extraction_remove_chunks_gin
"""

from alembic import op

revision = "US_extraction_batch_finalize"
down_revision = "US_extraction_remove_chunks_gin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION public.finish_document_processing_batch(
          p_tenant uuid,p_items jsonb
        ) RETURNS integer LANGUAGE plpgsql SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE item jsonb; completed integer := 0;
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          IF jsonb_typeof(p_items) <> 'array' OR jsonb_array_length(p_items) NOT BETWEEN 1 AND 8 THEN
            RAISE EXCEPTION 'finalization batch must contain 1 to 8 documents'
              USING ERRCODE='22023';
          END IF;
          FOR item IN SELECT value FROM jsonb_array_elements(p_items) LOOP
            IF NOT EXISTS (
              SELECT 1 FROM public.processing_jobs j
              WHERE j.tenant_id=p_tenant AND j.id=(item->>'job_id')::uuid
                AND j.lease_token=(item->>'lease')::uuid AND j.status='running'
                AND j.lease_until>clock_timestamp()
            ) THEN
              RAISE EXCEPTION 'processing lease expired' USING ERRCODE='40001';
            END IF;
            IF NOT public.finish_document_processing_cached(
              p_tenant,
              item->>'deal_id',
              (item->>'version_id')::uuid,
              item->>'content_hash',
              item->>'parser_version',
              item->'payload'
            ) THEN
              RAISE EXCEPTION 'document version not found' USING ERRCODE='P0002';
            END IF;
            completed := completed + 1;
          END LOOP;
          RETURN completed;
        END $$;
        COMMENT ON FUNCTION public.finish_document_processing_batch(uuid,jsonb) IS
          'Tenant and 1-8 fenced parsed document payloads -> completed count. Atomically finalizes and caches the batch in one tenant transaction.';
        REVOKE ALL ON FUNCTION public.finish_document_processing_batch(uuid,jsonb) FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.finish_document_processing_batch(uuid,jsonb)
              TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.finish_document_processing_batch(uuid,jsonb)")
