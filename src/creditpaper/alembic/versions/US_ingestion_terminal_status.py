"""Unify ingestion status, terminal progress, and worker health.

Revision ID: US_ingestion_terminal_status
Revises: 0045_out_of_authority, US_extraction_cache
"""

from alembic import op

revision = "US_ingestion_terminal_status"
down_revision = ("0045_out_of_authority", "US_extraction_cache")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Do not leave deployment hanging behind a long-lived API transaction.
    # PostgreSQL rolls the migration transaction back cleanly on timeout.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION public.deal_extraction_status_rows_v2(
          p_tenant_id uuid, p_deal_id text
        ) RETURNS TABLE (
          document_id uuid,
          version_id uuid,
          processing_status text,
          extraction_status text,
          reason_code text,
          retryable boolean
        )
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
          SELECT v.document_id,
                 v.id,
                 v.processing_status::text,
                 v.extraction_status::text,
                 CASE WHEN v.processing_error IS NULL THEN NULL
                      ELSE split_part(v.processing_error, ':', 1) END,
                 v.processing_status = 'failed'
          FROM public.deal_document_versions v
          JOIN public.deal_documents d
            ON d.id=v.document_id AND d.tenant_id=v.tenant_id
          WHERE v.tenant_id=p_tenant_id AND v.deal_id=p_deal_id
            AND v.superseded_at IS NULL AND d.archived_at IS NULL
            AND p_tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid
          ORDER BY v.uploaded_at, v.id
        $$;
        COMMENT ON FUNCTION public.deal_extraction_status_rows_v2(uuid,text) IS
          'Tenant/deal -> latest active document states, safe reason code, and retryability.';

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
          WITH states AS (
            SELECT v.processing_status::text AS status
            FROM public.deal_document_versions v
            JOIN public.deal_documents d
              ON d.id=v.document_id AND d.tenant_id=v.tenant_id
            WHERE v.tenant_id=p_tenant AND v.deal_id=p_deal
              AND v.superseded_at IS NULL AND d.archived_at IS NULL
          ), counts AS (
            SELECT count(*)::integer AS total,
              count(*) FILTER (WHERE status='pending')::integer AS queued,
              count(*) FILTER (WHERE status='processing')::integer AS processing,
              count(*) FILTER (WHERE status='ready')::integer AS ready,
              count(*) FILTER (WHERE status='failed')::integer AS failed
            FROM states
          )
          SELECT jsonb_build_object(
            'total',total,
            'queued',queued,
            'processing',processing,
            'ready',ready,
            'failed',failed,
            'terminal',ready+failed,
            'progress_percent',CASE WHEN total=0 THEN 100
              ELSE floor(((ready+failed)::numeric/total)*100)::integer END,
            'worker_alive',public.worker_alive(p_tenant),
            'worker_stale',(queued+processing)>0 AND NOT public.worker_alive(p_tenant)
          ) INTO result FROM counts;
          RETURN result;
        END $$;
        COMMENT ON FUNCTION public.processing_health(uuid,text) IS
          'Tenant/deal -> terminal progress counts and worker liveness in one read.';

        REVOKE ALL ON FUNCTION public.deal_extraction_status_rows_v2(uuid,text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.processing_health(uuid,text) FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.deal_extraction_status_rows_v2(uuid,text),
              public.processing_health(uuid,text) TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS public.deal_extraction_status_rows_v2(uuid,text);
        CREATE OR REPLACE FUNCTION public.processing_health(p_tenant uuid,p_deal text)
        RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          IF NOT EXISTS (SELECT 1 FROM public.deals WHERE tenant_id=p_tenant AND id=p_deal) THEN
            RETURN NULL;
          END IF;
          RETURN jsonb_build_object(
            'queued',(
              SELECT count(*)::integer
              FROM public.deal_document_versions v
              JOIN public.deal_documents d
                ON d.id=v.document_id AND d.tenant_id=v.tenant_id
              WHERE v.tenant_id=p_tenant AND v.deal_id=p_deal
                AND v.processing_status='pending' AND d.archived_at IS NULL
            ),
            'worker_alive',public.worker_alive(p_tenant)
          );
        END $$;
        COMMENT ON FUNCTION public.processing_health(uuid,text) IS
          'Tenant, deal -> queued document count and worker liveness.';
        """
    )
