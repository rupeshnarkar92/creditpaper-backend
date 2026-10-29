"""Read only the first three distinct pages used by proposal matching."""
from alembic import op

revision = "nn_144_match_opening_pages"
down_revision = "nn_143_bound_match_text"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE FUNCTION public.slot_match_pages(p_tenant uuid,p_deal text,p_versions uuid[])
    RETURNS TABLE(version_id uuid,page_texts jsonb)
    LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      SELECT e.version_id,COALESCE((SELECT jsonb_agg(jsonb_build_object('page',n,'text',txt) ORDER BY n)
        FROM (SELECT * FROM (
          SELECT DISTINCT ON ((p->>'page')::integer) (p->>'page')::integer AS n,
            COALESCE(p->>'text','') AS txt
          FROM jsonb_array_elements(COALESCE(e.page_texts::jsonb,'[]'::jsonb))
            WITH ORDINALITY AS pages(p,ord)
          WHERE p->'page' IS NOT NULL AND p->'page' NOT IN ('null'::jsonb,'0'::jsonb,'""'::jsonb,'false'::jsonb)
          ORDER BY (p->>'page')::integer,ord DESC
        ) distinct_pages ORDER BY n LIMIT 3) opening),'[]'::jsonb)
      FROM public.deal_document_extractions e
      JOIN public.deal_document_versions v ON v.id=e.version_id AND v.tenant_id=e.tenant_id
      WHERE e.tenant_id=p_tenant AND v.deal_id=p_deal AND v.id=ANY(p_versions)
        AND p_tenant=NULLIF(current_setting('app.tenant_id',true),'')::uuid
    $$;
    COMMENT ON FUNCTION public.slot_match_pages(uuid,text,uuid[]) IS
      'Tenant, deal, version IDs -> version ID and first three numerically ordered distinct pages. Last duplicate page wins, matching Python parsing. Full stored text is unchanged.';
    REVOKE ALL ON FUNCTION public.slot_match_pages(uuid,text,uuid[]) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.slot_match_pages(uuid,text,uuid[]) TO creditpaper_runtime;
    """)


def downgrade():
    op.execute("DROP FUNCTION public.slot_match_pages(uuid,text,uuid[])")
