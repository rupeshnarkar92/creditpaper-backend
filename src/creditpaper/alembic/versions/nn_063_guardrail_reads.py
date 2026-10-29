"""Move guardrail read queries behind stored functions."""

from alembic import op

revision = "nn_063_guardrail_reads"
down_revision = "nn_062_template_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.guardrail_rules_all(p_tenant_id uuid)
        RETURNS SETOF public.guardrail_rules LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT r.* FROM public.guardrail_rules r WHERE r.tenant_id=p_tenant_id ORDER BY r.created_at
        $$;
        CREATE OR REPLACE FUNCTION public.guardrail_rules_active(p_tenant_id uuid,p_module text)
        RETURNS SETOF public.guardrail_rules LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT r.* FROM public.guardrail_rules r WHERE r.tenant_id=p_tenant_id AND r.is_active=true
              AND (p_module IS NULL OR r.scope_type='global' OR (r.scope_type='module' AND r.scope_value=p_module)) ORDER BY r.created_at
        $$;
        CREATE OR REPLACE FUNCTION public.guardrail_rule_row(p_tenant_id uuid,p_id uuid)
        RETURNS SETOF public.guardrail_rules LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT r.* FROM public.guardrail_rules r WHERE r.tenant_id=p_tenant_id AND r.id=p_id $$;
        CREATE OR REPLACE FUNCTION public.guardrail_evaluations(p_tenant_id uuid,p_rule_id uuid,p_limit integer)
        RETURNS TABLE(id uuid,tenant_id uuid,rule_id uuid,subject_type text,subject_id text,result text,measured jsonb,evaluated_at timestamptz)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT e.id,e.tenant_id,e.rule_id,e.subject_type,e.subject_id,e.result,e.measured,e.evaluated_at FROM public.guardrail_evaluations e WHERE e.tenant_id=p_tenant_id AND e.rule_id=p_rule_id ORDER BY e.evaluated_at DESC LIMIT p_limit $$;
        REVOKE ALL ON FUNCTION public.guardrail_rules_all(uuid),public.guardrail_rules_active(uuid,text),public.guardrail_rule_row(uuid,uuid),public.guardrail_evaluations(uuid,uuid,integer) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.guardrail_rules_all(uuid),public.guardrail_rules_active(uuid,text),public.guardrail_rule_row(uuid,uuid),public.guardrail_evaluations(uuid,uuid,integer) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.guardrail_rules_all(uuid),public.guardrail_rules_active(uuid,text),public.guardrail_rule_row(uuid,uuid),public.guardrail_evaluations(uuid,uuid,integer)"
    )
