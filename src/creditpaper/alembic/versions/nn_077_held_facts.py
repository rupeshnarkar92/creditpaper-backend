"""Move held-facts upserts behind a stored function."""

from alembic import op

revision = "nn_077_held_facts"
down_revision = "nn_076_intake_confirm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.upsert_held_facts(p_tenant_id uuid,p_deal_id text,p_content_hash text,p_slot_key text,p_facts jsonb,p_coverage jsonb) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.ingest_held_facts(tenant_id,deal_id,content_hash,proposed_slot_key,facts,coverage) VALUES(p_tenant_id,p_deal_id,p_content_hash,p_slot_key,p_facts,p_coverage) ON CONFLICT(tenant_id,deal_id,content_hash) DO UPDATE SET proposed_slot_key=EXCLUDED.proposed_slot_key,facts=EXCLUDED.facts,coverage=EXCLUDED.coverage,created_at=now() $$; REVOKE ALL ON FUNCTION public.upsert_held_facts(uuid,text,text,text,jsonb,jsonb) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.upsert_held_facts(uuid,text,text,text,jsonb,jsonb) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.upsert_held_facts(uuid,text,text,text,jsonb,jsonb)")
