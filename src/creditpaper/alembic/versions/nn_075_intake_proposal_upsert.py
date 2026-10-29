"""Move intake proposal upserts behind a stored function."""

from alembic import op

revision = "nn_075_intake_proposal_upsert"
down_revision = "nn_074_deal_template_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION public.upsert_intake_proposal(
      p_tenant_id uuid,p_deal_id text,p_content_hash text,p_filename text,
      p_outcome text,p_slot_key text,p_confidence double precision,
      p_evidence jsonb,p_needs_party_context boolean
    ) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      INSERT INTO public.ingest_proposals
        (tenant_id,deal_id,content_hash,filename,outcome,slot_key,confidence,evidence,needs_party_context)
      VALUES (p_tenant_id,p_deal_id,p_content_hash,p_filename,p_outcome,p_slot_key,p_confidence,p_evidence,p_needs_party_context)
      ON CONFLICT (tenant_id,deal_id,content_hash) DO UPDATE SET
        filename=EXCLUDED.filename,outcome=EXCLUDED.outcome,slot_key=EXCLUDED.slot_key,
        confidence=EXCLUDED.confidence,evidence=EXCLUDED.evidence,
        needs_party_context=EXCLUDED.needs_party_context
    $$;
    REVOKE ALL ON FUNCTION public.upsert_intake_proposal(uuid,text,text,text,text,text,double precision,jsonb,boolean) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.upsert_intake_proposal(uuid,text,text,text,text,text,double precision,jsonb,boolean) TO creditpaper_runtime;
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.upsert_intake_proposal(uuid,text,text,text,text,text,double precision,jsonb,boolean)"
    )
