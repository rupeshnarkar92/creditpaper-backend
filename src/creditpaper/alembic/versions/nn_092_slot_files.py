"""Move slot-file listing behind a stored function."""

from alembic import op

revision = "nn_092_slot_files"
down_revision = "nn_091_slot_rows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.document_slot_file_rows(p_tenant_id uuid,p_deal_id text) RETURNS TABLE(slot_id uuid,document_id uuid,version_id uuid,role text,confidence numeric,match_reason text,created_at timestamptz) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT f.slot_id,f.document_id,f.version_id,f.role,f.confidence,f.match_reason,f.created_at FROM public.document_slot_files f JOIN public.document_slots s ON s.id=f.slot_id WHERE f.tenant_id=p_tenant_id AND s.tenant_id=p_tenant_id AND s.deal_id=p_deal_id ORDER BY f.created_at $$; REVOKE ALL ON FUNCTION public.document_slot_file_rows(uuid,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.document_slot_file_rows(uuid,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.document_slot_file_rows(uuid,text)")
