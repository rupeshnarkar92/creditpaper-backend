"""Revalidate bundle findings without retransferring unchanged page text.

Additive, no document writes. Deploy before the caller. Rollback the caller
before downgrading; dropping this function does not affect stored documents.
"""
from alembic import op

revision = "nn_145_bundle_check_inputs"
down_revision = "nn_144_match_opening_pages"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE FUNCTION public.slot_bundle_inputs(p_tenant uuid,p_versions uuid[],p_known jsonb)
    RETURNS TABLE(version_id uuid,fingerprint text,page_texts jsonb)
    LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      WITH inputs AS MATERIALIZED (
        SELECT e.version_id,COALESCE(e.page_texts::jsonb,'[]'::jsonb) AS pages,
          encode(sha256(convert_to(COALESCE(e.page_texts::jsonb,'[]'::jsonb)::text,'UTF8')),'hex') AS fp
        FROM public.deal_document_extractions e
        WHERE e.tenant_id=p_tenant AND e.version_id=ANY(p_versions)
          AND p_tenant=NULLIF(current_setting('app.tenant_id',true),'')::uuid
      )
      SELECT i.version_id,i.fp,
        CASE WHEN p_known->>i.version_id::text=i.fp THEN NULL::jsonb ELSE i.pages END
      FROM inputs i
    $$;
    COMMENT ON FUNCTION public.slot_bundle_inputs(uuid,uuid[],jsonb) IS
      'Tenant, version IDs, known version-to-SHA256 map -> version ID, current fingerprint, and complete page text only on mismatch. NULL pages means unchanged, absent rows mean unavailable. Rechecks tenant context and RLS on every call.';
    REVOKE ALL ON FUNCTION public.slot_bundle_inputs(uuid,uuid[],jsonb) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.slot_bundle_inputs(uuid,uuid[],jsonb) TO creditpaper_runtime;
    """)


def downgrade():
    op.execute("DROP FUNCTION public.slot_bundle_inputs(uuid,uuid[],jsonb)")
