"""Move the remaining extraction/review ledger DML behind stored functions."""

from alembic import op

revision = "nn_122_ledger_commit_helpers"
down_revision = "nn_121_derived_supersessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION public.ledger_superseded_version_ids(
        p_tenant_id uuid, p_deal_id text
    ) RETURNS TABLE(version_id text)
    LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
        SELECT v.id::text FROM public.deal_document_versions v
        WHERE v.tenant_id=p_tenant_id AND v.deal_id=p_deal_id
          AND v.superseded_at IS NOT NULL
    $$;
    COMMENT ON FUNCTION public.ledger_superseded_version_ids(uuid,text) IS
      'Returns superseded document-version IDs for a tenant deal; parameters are tenant and deal IDs.';
    REVOKE ALL ON FUNCTION public.ledger_superseded_version_ids(uuid,text) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.ledger_superseded_version_ids(uuid,text) TO creditpaper_runtime;

    CREATE OR REPLACE FUNCTION public.ledger_current_source_document(
        p_tenant_id uuid, p_deal_id text, p_version_id uuid
    ) RETURNS uuid
    LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
        SELECT v.document_id FROM public.deal_document_versions v
        JOIN public.deal_documents d ON d.id=v.document_id
        WHERE v.tenant_id=p_tenant_id AND v.deal_id=p_deal_id AND v.id=p_version_id
          AND d.archived_at IS NULL AND v.superseded_at IS NULL
    $$;
    COMMENT ON FUNCTION public.ledger_current_source_document(uuid,text,uuid) IS
      'Returns the document ID for a current, non-archived source version, or NULL; parameters are tenant, deal, and version IDs.';
    REVOKE ALL ON FUNCTION public.ledger_current_source_document(uuid,text,uuid) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.ledger_current_source_document(uuid,text,uuid) TO creditpaper_runtime;

    CREATE OR REPLACE FUNCTION public.enqueue_fact_review(
        p_tenant_id uuid, p_deal_id text, p_field_path text, p_document_id uuid,
        p_page integer, p_extracted_value jsonb, p_extracted_confidence double precision,
        p_assertion_id uuid
    ) RETURNS boolean
    LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    DECLARE affected integer;
    BEGIN
      INSERT INTO public.fact_review_queue
        (tenant_id,deal_id,field_path,document_id,page,extracted_value,extracted_confidence,assertion_id)
      VALUES
        (p_tenant_id,p_deal_id,p_field_path,p_document_id,p_page,p_extracted_value,p_extracted_confidence,p_assertion_id)
      ON CONFLICT(tenant_id,assertion_id) DO NOTHING;
      GET DIAGNOSTICS affected = ROW_COUNT;
      RETURN affected > 0;
    END $$;
    COMMENT ON FUNCTION public.enqueue_fact_review(uuid,text,text,uuid,integer,jsonb,double precision,uuid) IS
      'Queues an extracted assertion for review idempotently; returns true when a row was inserted.';
    REVOKE ALL ON FUNCTION public.enqueue_fact_review(uuid,text,text,uuid,integer,jsonb,double precision,uuid) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.enqueue_fact_review(uuid,text,text,uuid,integer,jsonb,double precision,uuid) TO creditpaper_runtime;

    CREATE OR REPLACE FUNCTION public.supersede_assertion_resolutions(
        p_tenant_id uuid, p_deal_id text, p_assertion_id uuid,
        p_recorded_at timestamptz, p_actor_id uuid, p_reason text
    ) RETURNS integer
    LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    DECLARE affected integer;
    BEGIN
      INSERT INTO public.ledger_supersessions
        (tenant_id,deal_id,resolution_id,recorded_at,actor_id,reason)
      SELECT r.tenant_id,r.deal_id,r.id,p_recorded_at,p_actor_id,p_reason
      FROM public.ledger_resolutions r
      WHERE r.tenant_id=p_tenant_id AND r.deal_id=p_deal_id AND r.assertion_id=p_assertion_id
        AND NOT EXISTS (
          SELECT 1 FROM public.ledger_supersessions s
          WHERE s.tenant_id=r.tenant_id AND s.resolution_id=r.id
        );
      GET DIAGNOSTICS affected = ROW_COUNT;
      RETURN affected;
    END $$;
    COMMENT ON FUNCTION public.supersede_assertion_resolutions(uuid,text,uuid,timestamptz,uuid,text) IS
      'Supersedes every unsuperseded resolution selecting an assertion; returns the inserted supersession count.';
    REVOKE ALL ON FUNCTION public.supersede_assertion_resolutions(uuid,text,uuid,timestamptz,uuid,text) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.supersede_assertion_resolutions(uuid,text,uuid,timestamptz,uuid,text) TO creditpaper_runtime;
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.supersede_assertion_resolutions(uuid,text,uuid,timestamptz,uuid,text)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.enqueue_fact_review(uuid,text,text,uuid,integer,jsonb,double precision,uuid)"
    )
    op.execute("DROP FUNCTION IF EXISTS public.ledger_current_source_document(uuid,text,uuid)")
    op.execute("DROP FUNCTION IF EXISTS public.ledger_superseded_version_ids(uuid,text)")
