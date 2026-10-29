"""Move intake confirmation writes behind a stored function."""

from alembic import op

revision = "nn_076_intake_confirm"
down_revision = "nn_075_intake_proposal_upsert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.upsert_intake_confirmation(p_tenant_id uuid,p_deal_id text,p_content_hash text,p_slot_key text,p_confirmed_by text) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.ingest_confirmations(tenant_id,deal_id,content_hash,slot_key,confirmed_by) VALUES(p_tenant_id,p_deal_id,p_content_hash,p_slot_key,p_confirmed_by) ON CONFLICT(tenant_id,deal_id,content_hash) DO UPDATE SET slot_key=EXCLUDED.slot_key,confirmed_by=EXCLUDED.confirmed_by,confirmed_at=now() $$; REVOKE ALL ON FUNCTION public.upsert_intake_confirmation(uuid,text,text,text,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.upsert_intake_confirmation(uuid,text,text,text,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.upsert_intake_confirmation(uuid,text,text,text,text)")
