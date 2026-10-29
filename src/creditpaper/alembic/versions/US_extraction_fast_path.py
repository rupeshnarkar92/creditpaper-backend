"""Batch worker lease renewal and extraction finalization/cache persistence.

Revision ID: US_extraction_fast_path
Revises: US_ingestion_review_reasons
"""

from alembic import op

revision = "US_extraction_fast_path"
down_revision = "US_ingestion_review_reasons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION public.refresh_worker_leases(p_tenant uuid,p_leases jsonb)
        RETURNS integer LANGUAGE plpgsql SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE refreshed integer;
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          PERFORM public.touch_worker_heartbeat(p_tenant);
          WITH supplied AS (
            SELECT x.id,x.lease
            FROM jsonb_to_recordset(COALESCE(p_leases,'[]'::jsonb)) AS x(id uuid,lease uuid)
          ), renewed AS (
            UPDATE public.processing_jobs j
            SET lease_until=clock_timestamp()+interval '90 seconds',
                updated_at=clock_timestamp()
            FROM supplied s
            WHERE j.tenant_id=p_tenant AND j.id=s.id AND j.lease_token=s.lease
              AND j.status='running' AND j.lease_until>clock_timestamp()
            RETURNING j.id
          ) SELECT count(*)::integer INTO refreshed FROM renewed;
          RETURN refreshed;
        END $$;
        COMMENT ON FUNCTION public.refresh_worker_leases(uuid,jsonb) IS
          'Tenant and active [{id,lease}] set -> number renewed; refreshes one worker heartbeat and every valid fenced lease in one transaction.';

        CREATE FUNCTION public.finish_document_processing_cached(
          p_tenant uuid,p_deal text,p_version uuid,p_content_sha256 text,
          p_parser_version text,p_data jsonb
        ) RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE found boolean;
        BEGIN
          found := public.finish_document_processing(p_tenant,p_deal,p_version,p_data);
          IF found AND p_content_sha256 IS NOT NULL THEN
            PERFORM public.put_processing_extraction_cache(
              p_tenant,p_content_sha256,p_parser_version,p_data
            );
          END IF;
          RETURN found;
        END $$;
        COMMENT ON FUNCTION public.finish_document_processing_cached(uuid,text,uuid,text,text,jsonb) IS
          'Tenant/deal/version plus optional content hash, parser version and extraction payload -> found boolean. Atomically finalizes the document and persists its reusable cache entry.';

        REVOKE ALL ON FUNCTION public.refresh_worker_leases(uuid,jsonb),
          public.finish_document_processing_cached(uuid,text,uuid,text,text,jsonb) FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.refresh_worker_leases(uuid,jsonb),
              public.finish_document_processing_cached(uuid,text,uuid,text,text,jsonb)
              TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS public.finish_document_processing_cached(uuid,text,uuid,text,text,jsonb);
        DROP FUNCTION IF EXISTS public.refresh_worker_leases(uuid,jsonb);
        """
    )
