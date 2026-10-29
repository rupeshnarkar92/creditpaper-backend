"""Read supersession evidence in one tenant/deal-scoped request."""

from alembic import op

revision = "nn_142_slot_match_inputs"
down_revision = "nn_141_slot_write_batch"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE FUNCTION public.slot_match_inputs(p_tenant uuid,p_deal text,p_versions uuid[])
    RETURNS TABLE(version_id uuid,content_hash text,full_text text)
    LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      SELECT v.id,v.content_sha256::text,COALESCE(e.full_text,'')
      FROM public.deal_document_versions v
      JOIN public.deal_documents d ON d.id=v.document_id AND d.tenant_id=v.tenant_id
      LEFT JOIN public.deal_document_extractions e ON e.version_id=v.id AND e.tenant_id=v.tenant_id
      WHERE v.tenant_id=p_tenant AND v.deal_id=p_deal AND d.deal_id=p_deal
        AND v.id=ANY(p_versions) AND d.archived_at IS NULL
        AND p_tenant=NULLIF(current_setting('app.tenant_id',true),'')::uuid
    $$;
    COMMENT ON FUNCTION public.slot_match_inputs(uuid,text,uuid[]) IS
      'Inputs tenant context, deal ID, eligible version IDs. Returns version ID, content hash and parsed full text for supersession checks. Missing/inaccessible versions return no rows.';
    REVOKE ALL ON FUNCTION public.slot_match_inputs(uuid,text,uuid[]) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.slot_match_inputs(uuid,text,uuid[]) TO creditpaper_runtime;
    """)


def downgrade():
    op.execute("DROP FUNCTION public.slot_match_inputs(uuid,text,uuid[])")
