"""Move ledger-aware document archival behind a stored function."""

from alembic import op

revision = "nn_112_ledger_archive"
down_revision = "nn_111_review_queue_correct"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION public.archive_ledger_document(
      p_tenant_id uuid,p_deal_id text,p_document_id uuid,p_actor_id uuid,p_recorded_at timestamptz
    ) RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    BEGIN
      UPDATE public.deal_documents SET archived_at=p_recorded_at
       WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id AND id=p_document_id AND archived_at IS NULL;
      IF NOT FOUND THEN RETURN false; END IF;
      INSERT INTO public.ledger_supersessions(tenant_id,deal_id,resolution_id,recorded_at,actor_id,reason)
      SELECT r.tenant_id,r.deal_id,r.id,p_recorded_at,p_actor_id,'source archived'
      FROM public.ledger_resolutions r JOIN public.ledger_assertions a
        ON a.tenant_id=r.tenant_id AND a.id=r.assertion_id
      JOIN public.deal_document_versions v ON v.tenant_id=a.tenant_id AND v.id=a.source_version_id
      WHERE r.tenant_id=p_tenant_id AND r.deal_id=p_deal_id AND v.document_id=p_document_id
        AND NOT EXISTS(SELECT 1 FROM public.ledger_supersessions s
          WHERE s.tenant_id=r.tenant_id AND s.resolution_id=r.id);
      RETURN true;
    END $$;
    REVOKE ALL ON FUNCTION public.archive_ledger_document(uuid,text,uuid,uuid,timestamptz) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.archive_ledger_document(uuid,text,uuid,uuid,timestamptz) TO creditpaper_runtime;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.archive_ledger_document(uuid,text,uuid,uuid,timestamptz)")
