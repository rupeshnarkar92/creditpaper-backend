"""Move slot status updates behind a stored function."""

from alembic import op

revision = "nn_100_slot_status"
down_revision = "nn_099_slot_confirmed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.update_document_slot(p_tenant_id uuid,p_deal_id text,p_id uuid,p_status text,p_confidence numeric,p_reason text,p_error text,p_updated_at timestamptz) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ WITH u AS (UPDATE public.document_slots SET status=p_status,confidence=p_confidence,match_reason=p_reason,error=p_error,updated_at=p_updated_at WHERE id=p_id AND tenant_id=p_tenant_id AND deal_id=p_deal_id RETURNING 1) SELECT EXISTS(SELECT 1 FROM u) $$; REVOKE ALL ON FUNCTION public.update_document_slot(uuid,text,uuid,text,numeric,text,text,timestamptz) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.update_document_slot(uuid,text,uuid,text,numeric,text,text,timestamptz) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.update_document_slot(uuid,text,uuid,text,numeric,text,text,timestamptz)"
    )
