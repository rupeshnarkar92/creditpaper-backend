"""Move guardrail partial updates behind a typed stored function."""

from alembic import op

revision = "nn_072_guardrail_update"
down_revision = "nn_071_event_write"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.update_guardrail_rule(p_tenant_id uuid,p_id uuid,p_payload jsonb,p_severity text,p_weight double precision,p_scope_type text,p_scope_value text,p_active boolean)
        RETURNS SETOF public.guardrail_rules LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            UPDATE public.guardrail_rules SET payload=COALESCE(p_payload,payload),severity=COALESCE(p_severity,severity),weight=COALESCE(p_weight,weight),scope_type=COALESCE(p_scope_type,scope_type),scope_value=COALESCE(p_scope_value,scope_value),is_active=COALESCE(p_active,is_active),updated_at=now()
            WHERE tenant_id=p_tenant_id AND id=p_id RETURNING *
        $$;
        REVOKE ALL ON FUNCTION public.update_guardrail_rule(uuid,uuid,jsonb,text,double precision,text,text,boolean) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.update_guardrail_rule(uuid,uuid,jsonb,text,double precision,text,text,boolean) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.update_guardrail_rule(uuid,uuid,jsonb,text,double precision,text,text,boolean)"
    )
