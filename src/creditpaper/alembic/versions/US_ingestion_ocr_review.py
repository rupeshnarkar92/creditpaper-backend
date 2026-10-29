"""Expose deferred OCR review work without blocking usable documents.

Revision ID: US_ingestion_ocr_review
Revises: US_ingestion_terminal_status
"""

from alembic import op

revision = "US_ingestion_ocr_review"
down_revision = "US_ingestion_terminal_status"
branch_labels = None
depends_on = None


def _function(*, include_deferred: bool) -> str:
    deferred_cte = """
      , review AS (
        SELECT count(*)::integer AS deferred_pages
        FROM active a
        JOIN public.deal_document_extractions e ON e.version_id=a.id AND e.tenant_id=p_tenant
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(e.page_texts,'[]'::jsonb)) page
        WHERE COALESCE((page->>'ocr_deferred')::boolean,false)
      )
    """ if include_deferred else ""
    deferred_pair = ",'deferred_pages',deferred_pages" if include_deferred else ""
    deferred_join = " CROSS JOIN review" if include_deferred else ""
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
      ) {deferred_cte}
      SELECT jsonb_build_object(
        'total',total,'queued',queued,'processing',processing,'ready',ready,'failed',failed,
        'terminal',ready+failed {deferred_pair},
        'progress_percent',CASE WHEN total=0 THEN 100
          ELSE floor(((ready+failed)::numeric/total)*100)::integer END,
        'worker_alive',public.worker_alive(p_tenant),
        'worker_stale',(queued+processing)>0 AND NOT public.worker_alive(p_tenant)
      ) INTO result FROM counts{deferred_join};
      RETURN result;
    END $$;
    """


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(_function(include_deferred=True))
    op.execute(
        "COMMENT ON FUNCTION public.processing_health(uuid,text) IS "
        "'Tenant/deal -> terminal progress, worker liveness, and pages deferred for OCR review.'"
    )


def downgrade() -> None:
    op.execute(_function(include_deferred=False))
