"""Consolidate processing start and completion. Deploy before worker restart.

No backfill. Revert callers before dropping these additive routines.
"""

from alembic import op

revision = "nn_148_processing_roundtrips"
down_revision = "nn_147_scoped_extraction_reset"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE FUNCTION public.begin_document_processing(p_tenant uuid,p_deal text,p_version uuid)
    RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    DECLARE v record;
    BEGIN
      IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
        RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
      END IF;
      SELECT storage_path,mime_type,document_id,version_number,original_filename,processing_status
        INTO v FROM public.deal_document_versions
        WHERE tenant_id=p_tenant AND deal_id=p_deal AND id=p_version;
      IF NOT FOUND THEN RETURN NULL; END IF;
      IF v.processing_status <> 'ready' THEN
        PERFORM public.set_document_processing_status(p_tenant,p_deal,p_version,'processing',NULL);
      END IF;
      RETURN to_jsonb(v);
    END $$;
    COMMENT ON FUNCTION public.begin_document_processing(uuid,text,uuid) IS
      'Tenant, deal, version -> metadata JSON (including prior processing_status), or NULL if absent. Tenant-checked atomic read/start; ready versions remain unchanged. Caller must fence the job in the same transaction.';

    CREATE FUNCTION public.finish_document_processing(p_tenant uuid,p_deal text,p_version uuid,p_data jsonb)
    RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    DECLARE v record;
    BEGIN
      IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
        RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
      END IF;
      SELECT document_id,version_number,mime_type INTO v FROM public.deal_document_versions
        WHERE tenant_id=p_tenant AND deal_id=p_deal AND id=p_version;
      IF NOT FOUND THEN RETURN false; END IF;
      PERFORM public.upsert_document_extraction(p_tenant,p_deal,v.document_id,p_version,
        v.version_number,v.mime_type,p_data->>'full_text',p_data->'page_texts',p_data->'chunks',
        (p_data->>'page_count')::integer,(p_data->>'sheet_count')::integer,
        (p_data->>'char_count')::integer,p_data->'classification');
      INSERT INTO public.ledger_chunks
        (tenant_id,deal_id,version_id,page,location,chunk_index,content,content_hash)
        SELECT p_tenant,p_deal,p_version,x.page,x.location,x.chunk_index,x.content,x.content_hash
        FROM jsonb_to_recordset(p_data->'index_chunks') AS x(
          page integer,location text,chunk_index integer,content text,content_hash text)
        ON CONFLICT DO NOTHING;
      IF COALESCE(p_data->'classification'->>'kind','') <> '' THEN
        PERFORM public.set_document_kind_if_unset(p_tenant,p_deal,v.document_id,
          p_data->'classification'->>'kind');
      END IF;
      PERFORM public.set_document_processing_status(p_tenant,p_deal,p_version,'ready',NULL);
      RETURN true;
    END $$;
    COMMENT ON FUNCTION public.finish_document_processing(uuid,text,uuid,jsonb) IS
      'Tenant, deal, version, extraction JSON (full_text/page_texts/chunks/page_count/sheet_count/char_count/classification/index_chunks) -> found boolean. Atomically saves extraction, bulk retrieval chunks, first-write kind and ready status. Caller must fence the job in the same transaction; document metadata is loaded server-side.';
    REVOKE ALL ON FUNCTION public.begin_document_processing(uuid,text,uuid) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.finish_document_processing(uuid,text,uuid,jsonb) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.begin_document_processing(uuid,text,uuid),
      public.finish_document_processing(uuid,text,uuid,jsonb) TO creditpaper_runtime;
    DO $$ DECLARE r text; BEGIN
      FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
          EXECUTE format('REVOKE ALL ON FUNCTION public.begin_document_processing(uuid,text,uuid) FROM %I',r);
          EXECUTE format('REVOKE ALL ON FUNCTION public.finish_document_processing(uuid,text,uuid,jsonb) FROM %I',r);
        END IF;
      END LOOP;
    END $$;
    """)


def downgrade():
    op.execute("DROP FUNCTION public.finish_document_processing(uuid,text,uuid,jsonb)")
    op.execute("DROP FUNCTION public.begin_document_processing(uuid,text,uuid)")
