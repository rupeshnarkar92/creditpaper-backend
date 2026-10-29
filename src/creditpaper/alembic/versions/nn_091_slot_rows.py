"""Move slot-row listing behind a stored function."""

from alembic import op

revision = "nn_091_slot_rows"
down_revision = "nn_090_integration_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.document_slot_rows(p_tenant_id uuid,p_deal_id text) RETURNS TABLE(id uuid,slot_key text,status text,confidence numeric,match_reason text,error text,updated_at timestamptz) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT id,slot_key,status,confidence,match_reason,error,updated_at FROM public.document_slots WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id $$; REVOKE ALL ON FUNCTION public.document_slot_rows(uuid,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.document_slot_rows(uuid,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.document_slot_rows(uuid,text)")
