"""Load processing metadata, content hash and extraction cache in one call.

Revision ID: US_extraction_start_fast_path
Revises: US_extraction_fast_path
"""

from alembic import op

revision = "US_extraction_start_fast_path"
down_revision = "US_extraction_fast_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION public.begin_document_processing_cached(
          p_tenant uuid,p_deal text,p_version uuid,p_parser_version text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE v record; cached jsonb;
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          SELECT storage_path,mime_type,document_id,version_number,original_filename,
                 processing_status,content_sha256
            INTO v FROM public.deal_document_versions
            WHERE tenant_id=p_tenant AND deal_id=p_deal AND id=p_version;
          IF NOT FOUND THEN RETURN NULL; END IF;
          IF v.processing_status <> 'ready' THEN
            PERFORM public.set_document_processing_status(
              p_tenant,p_deal,p_version,'processing',NULL
            );
          END IF;
          IF v.content_sha256 IS NOT NULL THEN
            SELECT payload INTO cached FROM public.processing_extraction_cache
            WHERE tenant_id=p_tenant AND content_sha256=v.content_sha256
              AND parser_version=p_parser_version;
          END IF;
          RETURN to_jsonb(v) || jsonb_build_object('cached_extraction',cached);
        END $$;
        COMMENT ON FUNCTION public.begin_document_processing_cached(uuid,text,uuid,text) IS
          'Tenant/deal/version/parser -> scoped document metadata, content hash and reusable extraction in one fenced transaction; marks non-ready versions processing.';
        REVOKE ALL ON FUNCTION public.begin_document_processing_cached(uuid,text,uuid,text)
          FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.begin_document_processing_cached(uuid,text,uuid,text)
              TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.begin_document_processing_cached(uuid,text,uuid,text)")
