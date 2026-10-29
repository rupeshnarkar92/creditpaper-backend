"""Move intake proposal and held-fact reads behind stored functions."""

from alembic import op

revision = "nn_064_intake_reads"
down_revision = "nn_063_guardrail_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.intake_proposals(p_tenant_id uuid,p_deal_id text)
        RETURNS TABLE(content_hash text,filename text,outcome text,slot_key text,confidence double precision,evidence jsonb,needs_party_context boolean,confirmed_slot_key text)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT p.content_hash,p.filename,p.outcome,p.slot_key,p.confidence,p.evidence,p.needs_party_context,c.slot_key
            FROM public.ingest_proposals p LEFT JOIN public.ingest_confirmations c ON c.tenant_id=p.tenant_id AND c.deal_id=p.deal_id AND c.content_hash=p.content_hash
            WHERE p.tenant_id=p_tenant_id AND p.deal_id=p_deal_id ORDER BY p.slot_key NULLS LAST,p.filename
        $$;
        CREATE OR REPLACE FUNCTION public.confirmed_intake_labels(p_tenant_id uuid)
        RETURNS TABLE(content_hash text,slot_key text) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT c.content_hash,c.slot_key FROM public.ingest_confirmations c WHERE c.tenant_id=p_tenant_id ORDER BY c.confirmed_at DESC $$;
        CREATE OR REPLACE FUNCTION public.labelled_intake_corpus(p_tenant_id uuid,p_limit integer)
        RETURNS TABLE(pages jsonb,slot_key text) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT a.pages,c.slot_key FROM public.ingest_confirmations c JOIN public.ingest_parse_artifacts a ON a.tenant_id=c.tenant_id AND a.content_hash=c.content_hash WHERE c.tenant_id=p_tenant_id ORDER BY c.confirmed_at DESC LIMIT p_limit $$;
        CREATE OR REPLACE FUNCTION public.held_facts_row(p_tenant_id uuid,p_deal_id text,p_hash text)
        RETURNS TABLE(proposed_slot_key text,facts jsonb,coverage jsonb) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT h.proposed_slot_key,h.facts,h.coverage FROM public.ingest_held_facts h WHERE h.tenant_id=p_tenant_id AND h.deal_id=p_deal_id AND h.content_hash=p_hash $$;
        REVOKE ALL ON FUNCTION public.intake_proposals(uuid,text),public.confirmed_intake_labels(uuid),public.labelled_intake_corpus(uuid,integer),public.held_facts_row(uuid,text,text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.intake_proposals(uuid,text),public.confirmed_intake_labels(uuid),public.labelled_intake_corpus(uuid,integer),public.held_facts_row(uuid,text,text) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.intake_proposals(uuid,text),public.confirmed_intake_labels(uuid),public.labelled_intake_corpus(uuid,integer),public.held_facts_row(uuid,text,text)"
    )
