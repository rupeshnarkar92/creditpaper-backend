"""Move ledger review existence check into a stored function."""

from alembic import op

revision = "nn_082_ledger_review_check"
down_revision = "nn_081_ledger_slot_rank"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.ledger_review_exists(p_tenant_id uuid,p_assertion_id uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT EXISTS(SELECT 1 FROM public.ledger_reviews WHERE tenant_id=p_tenant_id AND assertion_id=p_assertion_id) $$; REVOKE ALL ON FUNCTION public.ledger_review_exists(uuid,uuid) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.ledger_review_exists(uuid,uuid) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.ledger_review_exists(uuid,uuid)")
