"""Move ledger authority slot lookup into a stored function."""

from alembic import op

revision = "nn_081_ledger_slot_rank"
down_revision = "nn_080_ledger_lookups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.ledger_document_slots(p_tenant_id uuid,p_document_id text) RETURNS TABLE(slot_key text) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT s.slot_key FROM public.document_slot_files f JOIN public.document_slots s ON s.id=f.slot_id WHERE f.tenant_id=p_tenant_id AND f.document_id::text=p_document_id AND f.role='assigned' $$; REVOKE ALL ON FUNCTION public.ledger_document_slots(uuid,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.ledger_document_slots(uuid,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.ledger_document_slots(uuid,text)")
