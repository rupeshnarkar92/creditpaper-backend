"""Persist folder transfer locks and derive document processing readiness."""

from alembic import op

revision = "nn_137_ingestion_gate"
down_revision = "nn_136_fast_document_upload"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE public.deal_upload_batches (
      id uuid PRIMARY KEY, tenant_id uuid NOT NULL, deal_id text NOT NULL,
      total integer NOT NULL CHECK(total > 0), failed integer NOT NULL DEFAULT 0,
      sealed boolean NOT NULL DEFAULT false,
      updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY(tenant_id,deal_id) REFERENCES public.deals(tenant_id,id)
    );
    ALTER TABLE public.deal_upload_batches ENABLE ROW LEVEL SECURITY;
    ALTER TABLE public.deal_upload_batches FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON public.deal_upload_batches
      USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
      WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
    CREATE INDEX ON public.deal_upload_batches(tenant_id,deal_id);
    ALTER TABLE public.deal_upload_batches ADD COLUMN ignored_versions uuid[] NOT NULL DEFAULT '{}';

    CREATE FUNCTION public.deal_upload_batch_write(p_tenant uuid,p_deal text,p_id uuid,
      p_total integer,p_failed integer,p_sealed boolean) RETURNS void
    LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    BEGIN
      PERFORM 1 FROM public.deals WHERE tenant_id=p_tenant AND id=p_deal FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Deal not found'; END IF;
      IF EXISTS(SELECT 1 FROM public.processing_jobs WHERE tenant_id=p_tenant
        AND deal_id=p_deal AND kind='generation' AND status IN ('pending','running'))
      THEN RAISE EXCEPTION 'Paper generation is active'; END IF;
      INSERT INTO public.deal_upload_batches(id,tenant_id,deal_id,total,failed,sealed)
      VALUES(p_id,p_tenant,p_deal,p_total,p_failed,p_sealed)
      ON CONFLICT(id) DO UPDATE SET updated_at=clock_timestamp(),
        failed=CASE WHEN deal_upload_batches.sealed THEN deal_upload_batches.failed ELSE EXCLUDED.failed END,
        sealed=deal_upload_batches.sealed OR EXCLUDED.sealed
      WHERE deal_upload_batches.tenant_id=p_tenant AND deal_upload_batches.deal_id=p_deal;
    END $$;

    CREATE FUNCTION public.deal_ingestion_status(p_tenant uuid,p_deal text) RETURNS jsonb
    LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      WITH versions AS (
        SELECT v.processing_status,v.extraction_status FROM public.deal_document_versions v
        JOIN public.deal_documents d ON d.id=v.document_id AND d.tenant_id=v.tenant_id
        WHERE v.tenant_id=p_tenant AND v.deal_id=p_deal
          AND v.superseded_at IS NULL AND d.archived_at IS NULL
          AND NOT ((v.processing_status='failed' OR v.extraction_status='failed')
            AND v.processing_status NOT IN ('pending','processing') AND v.extraction_status<>'processing'
            AND EXISTS(SELECT 1 FROM public.deal_upload_batches b
            WHERE b.tenant_id=p_tenant AND b.deal_id=p_deal AND v.id=ANY(b.ignored_versions)))
      ), counts AS (
        SELECT count(*) AS total,
          count(*) FILTER(WHERE processing_status IN ('pending','processing')
            OR extraction_status='processing') AS pending,
          count(*) FILTER(WHERE processing_status='failed' OR extraction_status='failed') AS failed
        FROM versions
      ), batches AS (
        SELECT count(*) FILTER(WHERE NOT sealed) AS uploading,
          COALESCE(sum(failed),0) AS failed FROM public.deal_upload_batches
        WHERE tenant_id=p_tenant AND deal_id=p_deal
      ), jobs AS (
        SELECT count(*) AS pending FROM public.processing_jobs
        WHERE tenant_id=p_tenant AND deal_id=p_deal AND status IN ('pending','running')
          AND kind IN ('parse','document','extraction','corpus_extraction',
            'ingest_window','ingest_speculate','ingest_propose')
      ) SELECT jsonb_build_object('locked',
        c.pending>0 OR b.uploading>0 OR j.pending>0 OR c.failed+b.failed>0,
        'uploading',b.uploading,'pending',c.pending,'jobs',j.pending,
        'failed',c.failed+b.failed,'total',c.total,
        'complete',c.total-c.pending-c.failed)
    FROM counts c CROSS JOIN batches b CROSS JOIN jobs j
    $$;
    CREATE FUNCTION public.deal_ingestion_acknowledge(p_tenant uuid,p_deal text) RETURNS void
    LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    BEGIN
      IF EXISTS(SELECT 1 FROM public.deal_upload_batches WHERE tenant_id=p_tenant
        AND deal_id=p_deal AND NOT sealed AND updated_at>clock_timestamp()-interval '5 minutes')
      THEN RAISE EXCEPTION 'Upload still active'; END IF;
      UPDATE public.deal_upload_batches SET sealed=true,failed=0
      WHERE tenant_id=p_tenant AND deal_id=p_deal;
      INSERT INTO public.deal_upload_batches(id,tenant_id,deal_id,total,sealed,ignored_versions)
      SELECT gen_random_uuid(),p_tenant,p_deal,1,true,COALESCE(array_agg(v.id),'{}'::uuid[])
      FROM public.deal_document_versions v
      WHERE v.tenant_id=p_tenant AND v.deal_id=p_deal
        AND (v.processing_status='failed' OR v.extraction_status='failed');
    END $$;
    COMMENT ON FUNCTION public.deal_ingestion_acknowledge(uuid,text)
      IS 'Explicitly acknowledge failed documents and abandoned transfers older than five minutes for this tenant deal.';
    REVOKE ALL ON FUNCTION public.deal_ingestion_acknowledge(uuid,text) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.deal_ingestion_acknowledge(uuid,text) TO creditpaper_runtime;
    COMMENT ON FUNCTION public.deal_upload_batch_write(uuid,text,uuid,integer,integer,boolean)
      IS 'Start/update/seal a folder transfer. Parameters: tenant, deal, batch UUID, file count, failures, sealed. No return value.';
    COMMENT ON FUNCTION public.deal_ingestion_status(uuid,text)
      IS 'Return tenant deal upload, processing, failure counts and locked flag as JSON. Failed or interrupted uploads stay locked until resolved.';
    REVOKE ALL ON TABLE public.deal_upload_batches FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.deal_upload_batch_write(uuid,text,uuid,integer,integer,boolean),
      public.deal_ingestion_status(uuid,text) FROM PUBLIC;
    GRANT SELECT,INSERT,UPDATE ON public.deal_upload_batches TO creditpaper_runtime;
    GRANT EXECUTE ON FUNCTION public.deal_upload_batch_write(uuid,text,uuid,integer,integer,boolean),
      public.deal_ingestion_status(uuid,text) TO creditpaper_runtime;
    """)


def downgrade():
    raise RuntimeError("Upload history must be retained")
