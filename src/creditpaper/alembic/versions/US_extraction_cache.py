"""Tenant-scoped, parser-versioned extraction cache."""

from alembic import op

revision = "US_extraction_cache"
down_revision = "US_worker_heartbeats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE public.processing_extraction_cache (
      tenant_id uuid NOT NULL,
      content_sha256 text NOT NULL CHECK (length(content_sha256)=64),
      parser_version text NOT NULL,
      payload jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY (tenant_id, content_sha256, parser_version)
    );
    ALTER TABLE public.processing_extraction_cache ENABLE ROW LEVEL SECURITY;
    ALTER TABLE public.processing_extraction_cache FORCE ROW LEVEL SECURITY;
    CREATE POLICY processing_extraction_cache_tenant ON public.processing_extraction_cache
      USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

    CREATE FUNCTION public.get_processing_extraction_cache(
      p_tenant uuid, p_content_sha256 text, p_parser_version text
    ) RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
      SET search_path=pg_catalog,public AS $$
      SELECT payload FROM public.processing_extraction_cache
      WHERE tenant_id=p_tenant AND content_sha256=p_content_sha256 AND parser_version=p_parser_version
    $$;

    CREATE FUNCTION public.document_content_sha256(p_tenant uuid, p_deal text, p_version uuid)
    RETURNS text LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      SELECT content_sha256 FROM public.deal_document_versions
      WHERE tenant_id=p_tenant AND deal_id=p_deal AND id=p_version
    $$;

    CREATE FUNCTION public.put_processing_extraction_cache(
      p_tenant uuid, p_content_sha256 text, p_parser_version text, p_payload jsonb
    ) RETURNS void LANGUAGE sql SECURITY INVOKER
      SET search_path=pg_catalog,public AS $$
      INSERT INTO public.processing_extraction_cache(tenant_id,content_sha256,parser_version,payload)
      VALUES(p_tenant,p_content_sha256,p_parser_version,p_payload)
      ON CONFLICT(tenant_id,content_sha256,parser_version) DO UPDATE
        SET payload=EXCLUDED.payload, created_at=clock_timestamp()
    $$;
    COMMENT ON FUNCTION public.get_processing_extraction_cache(uuid,text,text) IS
      'Tenant, content hash, parser version -> complete extraction payload or NULL.';
    COMMENT ON FUNCTION public.put_processing_extraction_cache(uuid,text,text,jsonb) IS
      'Tenant, content hash, parser version, payload -> upserts a reusable extraction result.';
    REVOKE ALL ON TABLE public.processing_extraction_cache FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.get_processing_extraction_cache(uuid,text,text) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.document_content_sha256(uuid,text,uuid) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.put_processing_extraction_cache(uuid,text,text,jsonb) FROM PUBLIC;
    DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
        GRANT SELECT, INSERT, UPDATE ON public.processing_extraction_cache TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.get_processing_extraction_cache(uuid,text,text),
          public.put_processing_extraction_cache(uuid,text,text,jsonb),
          public.document_content_sha256(uuid,text,uuid) TO creditpaper_runtime;
      END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.put_processing_extraction_cache(uuid,text,text,jsonb)")
    op.execute("DROP FUNCTION IF EXISTS public.get_processing_extraction_cache(uuid,text,text)")
    op.execute("DROP FUNCTION IF EXISTS public.document_content_sha256(uuid,text,uuid)")
    op.execute("DROP TABLE IF EXISTS public.processing_extraction_cache")
