"""Move guardrail accuracy aggregation behind a stored function."""

from alembic import op

revision = "nn_093_guardrail_accuracy"
down_revision = "nn_092_slot_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.guardrail_accuracy_rows(p_tenant_id uuid) RETURNS TABLE(id uuid,kind text,weight numeric,total bigint,passes bigint,flags bigint) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT r.id,r.kind,r.weight,COUNT(e.id),COUNT(e.id) FILTER(WHERE e.result='pass'),COUNT(e.id) FILTER(WHERE e.result='flagged') FROM public.guardrail_rules r LEFT JOIN public.guardrail_evaluations e ON e.rule_id=r.id AND e.tenant_id=r.tenant_id WHERE r.tenant_id=p_tenant_id AND r.is_active=true GROUP BY r.id,r.kind,r.weight,r.created_at ORDER BY r.created_at $$; REVOKE ALL ON FUNCTION public.guardrail_accuracy_rows(uuid) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.guardrail_accuracy_rows(uuid) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.guardrail_accuracy_rows(uuid)")
