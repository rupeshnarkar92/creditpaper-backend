"""Durable ZIP intake with resumable worker checkpoints and tenant-scoped progress."""

from alembic import op

revision = "nn_139_archive_ingestion"
down_revision = "nn_138_batch_seal_guard"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
    ALTER TABLE public.deal_upload_batches ADD COLUMN archive_path text,
      ADD COLUMN items jsonb NOT NULL DEFAULT '{}';
    ALTER TABLE public.processing_jobs DROP CONSTRAINT processing_jobs_kind_check;
    ALTER TABLE public.processing_jobs ADD CONSTRAINT processing_jobs_kind_check CHECK(kind IN (
      'generation','extraction','document','parse','corpus_extraction',
      'ingest_window','ingest_speculate','ingest_propose','archive_import','archive_match'));

    CREATE FUNCTION public.accept_document_archive(p_tenant uuid,p_deal text,p_batch uuid,
      p_actor uuid,p_path text,p_count integer) RETURNS uuid
    LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    DECLARE job_id uuid;
    BEGIN
      PERFORM 1 FROM public.deals WHERE tenant_id=p_tenant AND id=p_deal FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Deal not found'; END IF;
      IF EXISTS(SELECT 1 FROM public.processing_jobs WHERE tenant_id=p_tenant AND deal_id=p_deal
        AND kind='generation' AND status IN ('pending','running'))
      THEN RAISE EXCEPTION 'Generation is active'; END IF;
      UPDATE public.deal_upload_batches SET archive_path=p_path,total=p_count,updated_at=clock_timestamp()
        WHERE tenant_id=p_tenant AND deal_id=p_deal AND id=p_batch AND NOT sealed AND archive_path IS NULL;
      IF NOT FOUND THEN RAISE EXCEPTION 'Batch unavailable or already accepted'; END IF;
      INSERT INTO public.processing_jobs(tenant_id,deal_id,kind,resource_id,payload)
        VALUES(p_tenant,p_deal,'archive_import',p_batch::text,
          jsonb_build_object('batch_id',p_batch,'actor_id',p_actor)) RETURNING id INTO job_id;
      RETURN job_id;
    END $$;

    CREATE FUNCTION public.document_archive(p_tenant uuid,p_deal text,p_batch uuid) RETURNS jsonb
    LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      SELECT jsonb_build_object('path',archive_path,'items',items,'total',total)
      FROM public.deal_upload_batches WHERE tenant_id=p_tenant AND deal_id=p_deal AND id=p_batch
    $$;
    CREATE FUNCTION public.document_archive_checkpoint(p_tenant uuid,p_deal text,p_batch uuid,
      p_items jsonb,p_sealed boolean) RETURNS void
    LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      UPDATE public.deal_upload_batches SET items=p_items,sealed=p_sealed,
        failed=(SELECT count(*) FROM jsonb_each(p_items) e WHERE e.value->>'status'='failed'),
        updated_at=clock_timestamp()
      WHERE tenant_id=p_tenant AND deal_id=p_deal AND id=p_batch
    $$;
    CREATE FUNCTION public.document_archive_progress(p_tenant uuid,p_deal text) RETURNS jsonb
    LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      SELECT COALESCE(jsonb_agg(jsonb_build_object('id',id,'total',total,'sealed',sealed,
        'processed',jsonb_object_length_safe(items),'failed',failed,
        'errors',(SELECT COALESCE(jsonb_agg(jsonb_build_object('name',e.key,'message',e.value->>'message')),'[]')
          FROM jsonb_each(items) e WHERE e.value->>'status'='failed')) ORDER BY updated_at DESC),'[]')
      FROM public.deal_upload_batches WHERE tenant_id=p_tenant AND deal_id=p_deal AND archive_path IS NOT NULL
    $$;
    """.replace("jsonb_object_length_safe(items)", "(SELECT count(*) FROM jsonb_each(items))")
    )
    op.execute("""
    CREATE FUNCTION public.retry_document_archive(p_tenant uuid,p_deal text,p_batch uuid,p_actor uuid)
    RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    DECLARE job_id uuid;
    BEGIN
      PERFORM 1 FROM public.deals WHERE tenant_id=p_tenant AND id=p_deal FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Deal not found'; END IF;
      IF EXISTS(SELECT 1 FROM public.processing_jobs WHERE tenant_id=p_tenant AND deal_id=p_deal
        AND kind IN ('generation','archive_import','archive_match') AND status IN ('pending','running'))
      THEN RAISE EXCEPTION 'Processing active'; END IF;
      UPDATE public.deal_upload_batches SET sealed=false,failed=0,updated_at=clock_timestamp()
        WHERE tenant_id=p_tenant AND deal_id=p_deal AND id=p_batch AND archive_path IS NOT NULL;
      IF NOT FOUND THEN RAISE EXCEPTION 'Archive unavailable'; END IF;
      INSERT INTO public.processing_jobs(tenant_id,deal_id,kind,resource_id,payload)
        VALUES(p_tenant,p_deal,'archive_import',p_batch::text,
          jsonb_build_object('batch_id',p_batch,'actor_id',p_actor)) RETURNING id INTO job_id;
      RETURN job_id;
    END $$;
    COMMENT ON FUNCTION public.retry_document_archive(uuid,text,uuid,uuid) IS
      'Retry incomplete archive items for tenant/deal/batch and actor. Returns durable import job UUID.';
    ALTER FUNCTION public.deal_ingestion_acknowledge(uuid,text) RENAME TO deal_ingestion_acknowledge_v1;
    CREATE FUNCTION public.deal_ingestion_acknowledge(p_tenant uuid,p_deal text) RETURNS void
    LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    BEGIN
      IF EXISTS(SELECT 1 FROM public.processing_jobs WHERE tenant_id=p_tenant AND deal_id=p_deal
        AND kind IN ('archive_import','archive_match') AND status IN ('pending','running'))
      THEN RAISE EXCEPTION 'Background import still active'; END IF;
      PERFORM public.deal_ingestion_acknowledge_v1(p_tenant,p_deal);
    END $$;
    COMMENT ON FUNCTION public.accept_document_archive(uuid,text,uuid,uuid,text,integer) IS
      'Accept encrypted archive for a registered tenant deal batch, atomically enqueue import, return job UUID.';
    COMMENT ON FUNCTION public.document_archive(uuid,text,uuid) IS
      'Internal worker read: tenant, deal, batch -> encrypted path, item checkpoints, total.';
    COMMENT ON FUNCTION public.document_archive_checkpoint(uuid,text,uuid,jsonb,boolean) IS
      'Worker checkpoint: tenant, deal, batch, per-path outcomes, sealed -> void. Caller fences the job lease.';
    COMMENT ON FUNCTION public.document_archive_progress(uuid,text) IS
      'Tenant deal -> batch counts and per-file errors, never storage paths or document contents.';
    DO $$ DECLARE signature text; role_name text; BEGIN
      FOREACH signature IN ARRAY ARRAY[
        'public.accept_document_archive(uuid,text,uuid,uuid,text,integer)',
        'public.retry_document_archive(uuid,text,uuid,uuid)',
        'public.document_archive(uuid,text,uuid)',
        'public.document_archive_checkpoint(uuid,text,uuid,jsonb,boolean)',
        'public.document_archive_progress(uuid,text)',
        'public.deal_ingestion_acknowledge(uuid,text)'
      ] LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',signature);
        FOREACH role_name IN ARRAY ARRAY['anon','authenticated'] LOOP
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %I',signature,role_name);
          END IF;
        END LOOP;
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO creditpaper_runtime',signature);
      END LOOP;
    END $$;
    """)


def downgrade():
    raise RuntimeError("Retain durable import history")
