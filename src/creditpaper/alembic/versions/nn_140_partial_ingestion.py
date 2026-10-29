"""Allow partial success and expose durable batch job states without changing history."""
from alembic import op

revision = "nn_140_partial_ingestion"
down_revision = "nn_139_archive_ingestion"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER FUNCTION public.deal_ingestion_status(uuid,text) RENAME TO deal_ingestion_status_v139;
    CREATE FUNCTION public.deal_ingestion_status(p_tenant uuid,p_deal text) RETURNS jsonb
    LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      WITH active AS (
        SELECT count(*) AS n FROM public.processing_jobs
        WHERE tenant_id=p_tenant AND deal_id=p_deal AND status IN ('pending','running')
        AND kind<>'generation'
      ), transfers AS (
        SELECT count(*) AS n FROM public.deal_upload_batches
        WHERE tenant_id=p_tenant AND deal_id=p_deal AND NOT sealed
        AND archive_path IS NULL AND updated_at>now()-interval '5 minutes'
      ) SELECT public.deal_ingestion_status_v139(p_tenant,p_deal) ||
        jsonb_build_object('locked',a.n>0 OR t.n>0,'jobs',a.n,'uploading',t.n)
      FROM active a CROSS JOIN transfers t
    $$;
    ALTER FUNCTION public.document_archive_progress(uuid,text) RENAME TO document_archive_progress_v139;
    CREATE FUNCTION public.document_archive_progress(p_tenant uuid,p_deal text) RETURNS jsonb
    LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      SELECT COALESCE(jsonb_agg(b || jsonb_build_object(
        'active',EXISTS(SELECT 1 FROM public.processing_jobs j WHERE j.tenant_id=p_tenant
          AND j.deal_id=p_deal AND j.resource_id=b->>'id'
          AND j.kind IN ('archive_import','archive_match') AND j.status IN ('pending','running')),
        'state',COALESCE((SELECT j.status FROM public.processing_jobs j WHERE j.tenant_id=p_tenant
          AND j.deal_id=p_deal AND j.resource_id=b->>'id'
          AND j.kind IN ('archive_import','archive_match')
          ORDER BY j.created_at DESC LIMIT 1),'unknown'))),'[]')
      FROM jsonb_array_elements(public.document_archive_progress_v139(p_tenant,p_deal)) b
    $$;
    COMMENT ON FUNCTION public.deal_ingestion_status(uuid,text) IS
      'Tenant/deal -> readiness JSON. Only active jobs or live transfers block; failed files remain warnings.';
    COMMENT ON FUNCTION public.document_archive_progress(uuid,text) IS
      'Tenant/deal -> batch counts, file errors, active flag and latest durable job state.';
    REVOKE ALL ON FUNCTION public.deal_ingestion_status(uuid,text),public.document_archive_progress(uuid,text) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.deal_ingestion_status(uuid,text),public.document_archive_progress(uuid,text) TO creditpaper_runtime;
    """)


def downgrade():
    op.execute("""
    DROP FUNCTION public.deal_ingestion_status(uuid,text);
    ALTER FUNCTION public.deal_ingestion_status_v139(uuid,text) RENAME TO deal_ingestion_status;
    DROP FUNCTION public.document_archive_progress(uuid,text);
    ALTER FUNCTION public.document_archive_progress_v139(uuid,text) RENAME TO document_archive_progress;
    """)
