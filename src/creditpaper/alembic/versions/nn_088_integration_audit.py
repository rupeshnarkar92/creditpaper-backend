"""Move integration audit inserts behind a stored function."""

from alembic import op

revision = "nn_088_integration_audit"
down_revision = "nn_087_onboarding_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.record_integration_audit(p_tenant_id uuid,p_actor_id uuid,p_actor_service text,p_resource_type text,p_resource_id text,p_action text,p_details jsonb) RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.tenant_integration_audit_events(tenant_id,actor_id,actor_service,resource_type,resource_id,action,details) VALUES(p_tenant_id,p_actor_id,p_actor_service,p_resource_type,NULLIF(p_resource_id,'')::uuid,p_action,p_details) $$; REVOKE ALL ON FUNCTION public.record_integration_audit(uuid,uuid,text,text,text,text,jsonb) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.record_integration_audit(uuid,uuid,text,text,text,text,jsonb) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.record_integration_audit(uuid,uuid,text,text,text,text,jsonb)")
