"""Add a durable OCR lane for documents that fail the fast text path.

Revision ID: US_extraction_ocr_lane
Revises: US_extraction_batch_finalize
"""

from alembic import op

revision = "US_extraction_ocr_lane"
down_revision = "US_extraction_batch_finalize"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE public.processing_jobs DROP CONSTRAINT IF EXISTS processing_jobs_kind_check;
        ALTER TABLE public.processing_jobs ADD CONSTRAINT processing_jobs_kind_check CHECK(kind IN (
          'generation','extraction','document','corpus_extraction','parse','parse_ocr',
          'ingest_window','ingest_speculate','ingest_propose','archive_import','archive_match'));

        CREATE FUNCTION public.defer_document_to_ocr(
          p_tenant uuid,p_deal text,p_version uuid,p_payload jsonb
        ) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE jid uuid;
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          IF NOT public.set_document_processing_status(
            p_tenant,p_deal,p_version,'pending','waiting_ocr'
          ) THEN
            RAISE EXCEPTION 'document version not found' USING ERRCODE='P0002';
          END IF;
          INSERT INTO public.processing_jobs(
            tenant_id,deal_id,kind,resource_id,payload
          ) VALUES (
            p_tenant,p_deal,'parse_ocr',p_version::text,p_payload
          ) ON CONFLICT DO NOTHING RETURNING id INTO jid;
          IF jid IS NULL THEN
            SELECT id INTO jid FROM public.processing_jobs
            WHERE tenant_id=p_tenant AND deal_id=p_deal AND kind='parse_ocr'
              AND resource_id=p_version::text AND status IN ('pending','running')
            ORDER BY created_at DESC LIMIT 1;
          END IF;
          RETURN jid;
        END $$;
        COMMENT ON FUNCTION public.defer_document_to_ocr(uuid,text,uuid,jsonb) IS
          'Tenant/deal/version/payload -> active OCR job UUID. Atomically returns the version to pending and idempotently queues the bounded OCR lane.';
        REVOKE ALL ON FUNCTION public.defer_document_to_ocr(uuid,text,uuid,jsonb) FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.defer_document_to_ocr(uuid,text,uuid,jsonb)
              TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS public.defer_document_to_ocr(uuid,text,uuid,jsonb);
        ALTER TABLE public.processing_jobs DROP CONSTRAINT IF EXISTS processing_jobs_kind_check;
        ALTER TABLE public.processing_jobs ADD CONSTRAINT processing_jobs_kind_check CHECK(kind IN (
          'generation','extraction','document','corpus_extraction','parse',
          'ingest_window','ingest_speculate','ingest_propose','archive_import','archive_match'));
        """
    )
