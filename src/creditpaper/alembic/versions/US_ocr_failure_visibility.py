"""Expose page-level OCR failures as non-blocking review warnings.

Revision ID: US_ocr_failure_visibility
Revises: US_job_batch_finish
"""

from alembic import op

revision = "US_ocr_failure_visibility"
down_revision = "US_job_batch_finish"
branch_labels = None
depends_on = None


def _function(*, include_failed: bool) -> str:
    failed_pair = ",'ocr_failed_pages',ocr_failed_pages" if include_failed else ""
    failed_value = (
        "count(*) FILTER (WHERE COALESCE((page->>'ocr_failed')::boolean,false))::integer"
        if include_failed
        else "0::integer"
    )
    return f"""
    CREATE OR REPLACE FUNCTION public.processing_health(p_tenant uuid,p_deal text)
    RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER
    SET search_path=pg_catalog,public AS $$
    DECLARE result jsonb;
    BEGIN
      IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
        RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
      END IF;
      IF NOT EXISTS (SELECT 1 FROM public.deals WHERE tenant_id=p_tenant AND id=p_deal) THEN
        RETURN NULL;
      END IF;
      WITH active AS (
        SELECT v.id,v.processing_status::text AS status
        FROM public.deal_document_versions v
        JOIN public.deal_documents d ON d.id=v.document_id AND d.tenant_id=v.tenant_id
        WHERE v.tenant_id=p_tenant AND v.deal_id=p_deal
          AND v.superseded_at IS NULL AND d.archived_at IS NULL
      ), counts AS (
        SELECT count(*)::integer AS total,
          count(*) FILTER (WHERE status='pending')::integer AS queued,
          count(*) FILTER (WHERE status='processing')::integer AS processing,
          count(*) FILTER (WHERE status='ready')::integer AS ready,
          count(*) FILTER (WHERE status='failed')::integer AS failed
        FROM active
      ), review AS (
        SELECT
          count(*) FILTER (WHERE COALESCE((page->>'ocr_deferred')::boolean,false))::integer
            AS deferred_pages,
          {failed_value} AS ocr_failed_pages
        FROM active a
        JOIN public.deal_document_extractions e ON e.version_id=a.id AND e.tenant_id=p_tenant
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(e.page_texts,'[]'::jsonb)) page
      )
      SELECT jsonb_build_object(
        'total',total,'queued',queued,'processing',processing,'ready',ready,'failed',failed,
        'terminal',ready+failed,'deferred_pages',deferred_pages {failed_pair},
        'progress_percent',CASE WHEN total=0 THEN 100
          ELSE floor(((ready+failed)::numeric/total)*100)::integer END,
        'worker_alive',public.worker_alive(p_tenant),
        'worker_stale',(queued+processing)>0 AND NOT public.worker_alive(p_tenant)
      ) INTO result FROM counts CROSS JOIN review;
      RETURN result;
    END $$;
    """


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(_function(include_failed=True))
    op.execute(
        "COMMENT ON FUNCTION public.processing_health(uuid,text) IS "
        "'Tenant/deal terminal progress plus deferred and failed OCR pages. OCR warnings do not block usable evidence.'"
    )


def downgrade() -> None:
    op.execute(_function(include_failed=False))
