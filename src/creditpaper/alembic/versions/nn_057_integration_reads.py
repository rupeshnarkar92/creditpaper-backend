"""Move integration and MCP read queries behind tenant-scoped functions."""

from alembic import op

revision = "nn_057_integration_reads"
down_revision = "nn_056_onboard_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.list_tenant_integrations(p_tenant_id uuid)
        RETURNS TABLE(id uuid,name text,provider text,base_url text,auth_type text,config jsonb,enabled boolean,created_at timestamptz,updated_at timestamptz)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT i.id,i.name,i.provider,i.base_url,i.auth_type,i.config,i.enabled,i.created_at,i.updated_at
            FROM public.tenant_integrations i WHERE i.tenant_id=p_tenant_id ORDER BY i.name
        $$;
        CREATE OR REPLACE FUNCTION public.integration_runtime_row(p_tenant_id uuid,p_id uuid)
        RETURNS TABLE(id uuid,name text,provider text,base_url text,auth_type text,encrypted_credentials text,config jsonb,rate_limit_per_minute integer)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT i.id,i.name,i.provider,i.base_url,i.auth_type,i.encrypted_credentials,i.config,i.rate_limit_per_minute
            FROM public.tenant_integrations i WHERE i.tenant_id=p_tenant_id AND i.id=p_id AND i.enabled=true
        $$;
        CREATE OR REPLACE FUNCTION public.list_tenant_mcp_servers(p_tenant_id uuid)
        RETURNS TABLE(id uuid,name text,endpoint text,auth_type text,allowed_tools jsonb,allowed_resources jsonb,approved boolean,enabled boolean,created_at timestamptz,updated_at timestamptz)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT m.id,m.name,m.endpoint,m.auth_type,m.allowed_tools,m.allowed_resources,m.approved,m.enabled,m.created_at,m.updated_at
            FROM public.tenant_mcp_servers m WHERE m.tenant_id=p_tenant_id ORDER BY m.name
        $$;
        CREATE OR REPLACE FUNCTION public.webhook_integration_secret_row(p_tenant_id uuid,p_id uuid)
        RETURNS text LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT i.encrypted_credentials FROM public.tenant_integrations i
            WHERE i.tenant_id=p_tenant_id AND i.id=p_id AND i.enabled=true AND i.auth_type='webhook'
        $$;
        REVOKE ALL ON FUNCTION public.list_tenant_integrations(uuid),public.integration_runtime_row(uuid,uuid),public.list_tenant_mcp_servers(uuid),public.webhook_integration_secret_row(uuid,uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.list_tenant_integrations(uuid),public.integration_runtime_row(uuid,uuid),public.list_tenant_mcp_servers(uuid),public.webhook_integration_secret_row(uuid,uuid) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.list_tenant_integrations(uuid),public.integration_runtime_row(uuid,uuid),public.list_tenant_mcp_servers(uuid),public.webhook_integration_secret_row(uuid,uuid)"
    )
