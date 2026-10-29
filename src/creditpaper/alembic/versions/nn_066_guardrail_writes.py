"""Move guardrail create, deactivate, and evaluation writes behind functions."""

from alembic import op

revision = "nn_066_guardrail_writes"
down_revision = "nn_065_papers_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.create_guardrail_rule(p_id uuid,p_tenant_id uuid,p_kind text,p_payload jsonb,p_severity text,p_weight double precision,p_scope_type text,p_scope_value text)
        RETURNS SETOF public.guardrail_rules LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.guardrail_rules(id,tenant_id,kind,payload,severity,weight,scope_type,scope_value,is_active) VALUES(p_id,p_tenant_id,p_kind,p_payload,p_severity,p_weight,p_scope_type,p_scope_value,true) RETURNING * $$;
        CREATE OR REPLACE FUNCTION public.deactivate_guardrail_rule(p_tenant_id uuid,p_id uuid)
        RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ UPDATE public.guardrail_rules SET is_active=false,updated_at=now() WHERE tenant_id=p_tenant_id AND id=p_id AND is_active=true RETURNING true $$;
        CREATE OR REPLACE FUNCTION public.record_guardrail_evaluation(p_id uuid,p_tenant_id uuid,p_rule_id uuid,p_subject_type text,p_subject_id text,p_result text,p_measured jsonb)
        RETURNS TABLE(id uuid,tenant_id uuid,rule_id uuid,subject_type text,subject_id text,result text,measured jsonb,evaluated_at timestamptz)
        LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.guardrail_evaluations(id,tenant_id,rule_id,subject_type,subject_id,result,measured) VALUES(p_id,p_tenant_id,p_rule_id,p_subject_type,p_subject_id,p_result,p_measured) RETURNING id,tenant_id,rule_id,subject_type,subject_id,result,measured,evaluated_at $$;
        REVOKE ALL ON FUNCTION public.create_guardrail_rule(uuid,uuid,text,jsonb,text,double precision,text,text),public.deactivate_guardrail_rule(uuid,uuid),public.record_guardrail_evaluation(uuid,uuid,uuid,text,text,text,jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.create_guardrail_rule(uuid,uuid,text,jsonb,text,double precision,text,text),public.deactivate_guardrail_rule(uuid,uuid),public.record_guardrail_evaluation(uuid,uuid,uuid,text,text,text,jsonb) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.create_guardrail_rule(uuid,uuid,text,jsonb,text,double precision,text,text),public.deactivate_guardrail_rule(uuid,uuid),public.record_guardrail_evaluation(uuid,uuid,uuid,text,text,text,jsonb)"
    )
