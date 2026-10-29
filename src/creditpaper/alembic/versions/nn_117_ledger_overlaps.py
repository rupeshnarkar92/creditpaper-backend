"""Move ledger resolution overlap lookup behind a stored function."""

from alembic import op

revision = "nn_117_ledger_overlaps"
down_revision = "nn_116_ledger_assertion_row"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.ledger_resolution_overlaps(p_tenant_id uuid,p_deal_id text,p_path text,p_valid_from timestamptz,p_valid_to timestamptz) RETURNS TABLE(id uuid,assertion_id uuid,valid_from timestamptz,valid_to timestamptz) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT r.id,r.assertion_id,r.valid_from,r.valid_to FROM public.ledger_resolutions r WHERE r.tenant_id=p_tenant_id AND r.deal_id=p_deal_id AND r.field_path=p_path AND tstzrange(r.valid_from,r.valid_to,'[)') && tstzrange(p_valid_from,p_valid_to,'[)') AND NOT EXISTS(SELECT 1 FROM public.ledger_supersessions s WHERE s.tenant_id=r.tenant_id AND s.resolution_id=r.id) $$; REVOKE ALL ON FUNCTION public.ledger_resolution_overlaps(uuid,text,text,timestamptz,timestamptz) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.ledger_resolution_overlaps(uuid,text,text,timestamptz,timestamptz) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.ledger_resolution_overlaps(uuid,text,text,timestamptz,timestamptz)"
    )
