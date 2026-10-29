"""Return only the text window already consumed by supersession matching."""
from alembic import op

revision = "nn_143_bound_match_text"
down_revision = "nn_142_slot_match_inputs"
branch_labels = None
depends_on = None


def _replace(expression):
    op.execute(f"""
    CREATE OR REPLACE FUNCTION public.slot_match_inputs(p_tenant uuid,p_deal text,p_versions uuid[])
    RETURNS TABLE(version_id uuid,content_hash text,full_text text)
    LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      SELECT v.id,v.content_sha256::text,{expression}
      FROM public.deal_document_versions v
      JOIN public.deal_documents d ON d.id=v.document_id AND d.tenant_id=v.tenant_id
      LEFT JOIN public.deal_document_extractions e ON e.version_id=v.id AND e.tenant_id=v.tenant_id
      WHERE v.tenant_id=p_tenant AND v.deal_id=p_deal AND d.deal_id=p_deal
        AND v.id=ANY(p_versions) AND d.archived_at IS NULL
        AND p_tenant=NULLIF(current_setting('app.tenant_id',true),'')::uuid
    $$;
    """)


def upgrade():
    _replace("left(COALESCE(e.full_text,''),20000)")
    op.execute("""COMMENT ON FUNCTION public.slot_match_inputs(uuid,text,uuid[]) IS
      'Tenant, deal, eligible version IDs -> version ID, hash, first 20000 characters of parsed text. This is the families matcher evidence window, not full document content. Stored text is unchanged.'""")


def downgrade():
    _replace("COALESCE(e.full_text,'')")
