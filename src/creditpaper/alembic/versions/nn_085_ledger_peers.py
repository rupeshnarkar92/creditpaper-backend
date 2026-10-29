"""Move ledger peer assertion lookup into a stored function."""

from alembic import op

revision = "nn_085_ledger_peers"
down_revision = "nn_084_ledger_history_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.ledger_peer_assertions(p_tenant_id uuid,p_deal_id text,p_path text,p_version_id uuid) RETURNS TABLE(assertion_id text,fact jsonb) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT a.id::text,a.fact FROM public.ledger_assertions a WHERE a.tenant_id=p_tenant_id AND a.deal_id=p_deal_id AND a.field_path=p_path AND a.disposition IN ('accepted','review','legacy') AND a.source_version_id IS DISTINCT FROM p_version_id AND a.fact->'provenance'->>'method' <> 'derived' AND NOT EXISTS(SELECT 1 FROM public.ledger_reviews r WHERE r.tenant_id=a.tenant_id AND r.assertion_id=a.id AND r.action='reject') AND NOT EXISTS(SELECT 1 FROM public.deal_document_versions v JOIN public.deal_documents d ON d.id=v.document_id WHERE v.tenant_id=a.tenant_id AND v.id=a.source_version_id AND d.archived_at IS NOT NULL) ORDER BY a.recorded_at,a.id $$; REVOKE ALL ON FUNCTION public.ledger_peer_assertions(uuid,text,text,uuid) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.ledger_peer_assertions(uuid,text,text,uuid) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.ledger_peer_assertions(uuid,text,text,uuid)")
