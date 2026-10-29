"""Move remaining integration and MCP persistence behind tenant-scoped routines."""

from alembic import op

revision = "nn_123_integration_writes"
down_revision = "nn_122_ledger_commit_helpers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION public.update_tenant_integration(
      p_tenant_id uuid,p_id uuid,p_name text,p_provider text,p_base_url text,p_auth_type text,
      p_encrypted_credentials text,p_config jsonb,p_rate_limit integer
    ) RETURNS uuid LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      UPDATE public.tenant_integrations SET name=p_name,provider=p_provider,base_url=p_base_url,
        auth_type=p_auth_type,encrypted_credentials=p_encrypted_credentials,config=p_config,
        rate_limit_per_minute=p_rate_limit,enabled=true,updated_at=now()
      WHERE tenant_id=p_tenant_id AND id=p_id RETURNING id
    $$;
    COMMENT ON FUNCTION public.update_tenant_integration(uuid,uuid,text,text,text,text,text,jsonb,integer) IS
      'Updates one tenant integration and returns its ID, or NULL/no row when absent; parameters contain tenant ownership and sanitized integration configuration.';

    CREATE OR REPLACE FUNCTION public.create_tenant_integration(
      p_tenant_id uuid,p_name text,p_provider text,p_base_url text,p_auth_type text,
      p_encrypted_credentials text,p_config jsonb,p_rate_limit integer,p_created_by uuid
    ) RETURNS uuid LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      INSERT INTO public.tenant_integrations
        (tenant_id,name,provider,base_url,auth_type,encrypted_credentials,config,rate_limit_per_minute,created_by)
      VALUES(p_tenant_id,p_name,p_provider,p_base_url,p_auth_type,p_encrypted_credentials,p_config,p_rate_limit,p_created_by)
      RETURNING id
    $$;
    COMMENT ON FUNCTION public.create_tenant_integration(uuid,text,text,text,text,text,jsonb,integer,uuid) IS
      'Creates a tenant integration and returns its UUID; parameters contain tenant ownership, encrypted credentials, sanitized config, quota, and creator.';

    CREATE OR REPLACE FUNCTION public.accept_integration_webhook(
      p_tenant_id uuid,p_integration_id uuid,p_event_id text,p_payload_sha256 text
    ) RETURNS integer LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    DECLARE affected integer;
    BEGIN
      INSERT INTO public.tenant_integration_webhook_events
        (tenant_id,integration_id,event_id,payload_sha256)
      SELECT p_tenant_id,i.id,p_event_id,p_payload_sha256
      FROM public.tenant_integrations i
      WHERE i.tenant_id=p_tenant_id AND i.id=p_integration_id
        AND i.enabled=true AND i.auth_type='webhook'
      ON CONFLICT(tenant_id,integration_id,event_id) DO NOTHING;
      GET DIAGNOSTICS affected = ROW_COUNT;
      IF affected=1 THEN RETURN 1; END IF;
      RETURN CASE WHEN EXISTS (SELECT 1 FROM public.tenant_integrations
        WHERE tenant_id=p_tenant_id AND id=p_integration_id AND enabled=true AND auth_type='webhook')
        THEN 0 ELSE -1 END;
    END $$;
    COMMENT ON FUNCTION public.accept_integration_webhook(uuid,uuid,text,text) IS
      'Idempotently records an authenticated webhook; returns 1 accepted, 0 replay, or -1 when the enabled webhook integration is absent.';

    CREATE OR REPLACE FUNCTION public.mcp_server_credentials_for_update(p_tenant_id uuid,p_id uuid)
    RETURNS TABLE(encrypted_credentials text,approved boolean,auth_type text)
    LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      SELECT m.encrypted_credentials,m.approved,m.auth_type FROM public.tenant_mcp_servers m
      WHERE m.tenant_id=p_tenant_id AND m.id=p_id FOR UPDATE
    $$;
    COMMENT ON FUNCTION public.mcp_server_credentials_for_update(uuid,uuid) IS
      'Locks and returns encrypted credentials, approval, and auth type for one tenant MCP server; parameters are tenant and server IDs.';

    CREATE OR REPLACE FUNCTION public.update_tenant_mcp_server(
      p_tenant_id uuid,p_id uuid,p_name text,p_endpoint text,p_auth_type text,p_encrypted_credentials text,
      p_allowed_tools jsonb,p_allowed_resources jsonb
    ) RETURNS uuid LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      UPDATE public.tenant_mcp_servers SET name=p_name,endpoint=p_endpoint,auth_type=p_auth_type,
        encrypted_credentials=p_encrypted_credentials,allowed_tools=p_allowed_tools,
        allowed_resources=p_allowed_resources,approved=false,enabled=true,updated_at=now()
      WHERE tenant_id=p_tenant_id AND id=p_id RETURNING id
    $$;
    COMMENT ON FUNCTION public.update_tenant_mcp_server(uuid,uuid,text,text,text,text,jsonb,jsonb) IS
      'Updates and revokes approval for one tenant MCP server, returning its UUID; parameters contain tenant ownership and sanitized server configuration.';

    CREATE OR REPLACE FUNCTION public.create_tenant_mcp_server(
      p_tenant_id uuid,p_name text,p_endpoint text,p_auth_type text,p_encrypted_credentials text,
      p_allowed_tools jsonb,p_allowed_resources jsonb,p_created_by uuid
    ) RETURNS uuid LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      INSERT INTO public.tenant_mcp_servers
        (tenant_id,name,endpoint,auth_type,encrypted_credentials,allowed_tools,allowed_resources,created_by)
      VALUES(p_tenant_id,p_name,p_endpoint,p_auth_type,p_encrypted_credentials,p_allowed_tools,p_allowed_resources,p_created_by)
      RETURNING id
    $$;
    COMMENT ON FUNCTION public.create_tenant_mcp_server(uuid,text,text,text,text,jsonb,jsonb,uuid) IS
      'Creates a tenant MCP server and returns its UUID; parameters contain tenant ownership, encrypted credentials, allowlists, and creator.';

    CREATE OR REPLACE FUNCTION public.set_tenant_mcp_approval(p_tenant_id uuid,p_id uuid,p_approved boolean)
    RETURNS text LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      UPDATE public.tenant_mcp_servers SET approved=p_approved,updated_at=now()
      WHERE tenant_id=p_tenant_id AND id=p_id RETURNING name
    $$;
    COMMENT ON FUNCTION public.set_tenant_mcp_approval(uuid,uuid,boolean) IS
      'Sets approval for one tenant MCP server and returns its name, or NULL/no row when absent.';

    CREATE OR REPLACE FUNCTION public.delete_tenant_mcp_server(p_tenant_id uuid,p_id uuid)
    RETURNS text LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      DELETE FROM public.tenant_mcp_servers WHERE tenant_id=p_tenant_id AND id=p_id RETURNING name
    $$;
    COMMENT ON FUNCTION public.delete_tenant_mcp_server(uuid,uuid) IS
      'Deletes one tenant MCP server and returns its name, or NULL/no row when absent; parameters are tenant and server IDs.';

    REVOKE ALL ON FUNCTION public.update_tenant_integration(uuid,uuid,text,text,text,text,text,jsonb,integer) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.create_tenant_integration(uuid,text,text,text,text,text,jsonb,integer,uuid) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.accept_integration_webhook(uuid,uuid,text,text) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.mcp_server_credentials_for_update(uuid,uuid) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.update_tenant_mcp_server(uuid,uuid,text,text,text,text,jsonb,jsonb) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.create_tenant_mcp_server(uuid,text,text,text,text,jsonb,jsonb,uuid) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.set_tenant_mcp_approval(uuid,uuid,boolean) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.delete_tenant_mcp_server(uuid,uuid) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.update_tenant_integration(uuid,uuid,text,text,text,text,text,jsonb,integer) TO creditpaper_runtime;
    GRANT EXECUTE ON FUNCTION public.create_tenant_integration(uuid,text,text,text,text,text,jsonb,integer,uuid) TO creditpaper_runtime;
    GRANT EXECUTE ON FUNCTION public.accept_integration_webhook(uuid,uuid,text,text) TO creditpaper_runtime;
    GRANT EXECUTE ON FUNCTION public.mcp_server_credentials_for_update(uuid,uuid) TO creditpaper_runtime;
    GRANT EXECUTE ON FUNCTION public.update_tenant_mcp_server(uuid,uuid,text,text,text,text,jsonb,jsonb) TO creditpaper_runtime;
    GRANT EXECUTE ON FUNCTION public.create_tenant_mcp_server(uuid,text,text,text,text,jsonb,jsonb,uuid) TO creditpaper_runtime;
    GRANT EXECUTE ON FUNCTION public.set_tenant_mcp_approval(uuid,uuid,boolean) TO creditpaper_runtime;
    GRANT EXECUTE ON FUNCTION public.delete_tenant_mcp_server(uuid,uuid) TO creditpaper_runtime;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.delete_tenant_mcp_server(uuid,uuid)")
    op.execute("DROP FUNCTION IF EXISTS public.set_tenant_mcp_approval(uuid,uuid,boolean)")
    op.execute(
        "DROP FUNCTION IF EXISTS public.create_tenant_mcp_server(uuid,text,text,text,text,jsonb,jsonb,uuid)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.update_tenant_mcp_server(uuid,uuid,text,text,text,text,jsonb,jsonb)"
    )
    op.execute("DROP FUNCTION IF EXISTS public.mcp_server_credentials_for_update(uuid,uuid)")
    op.execute("DROP FUNCTION IF EXISTS public.accept_integration_webhook(uuid,uuid,text,text)")
    op.execute(
        "DROP FUNCTION IF EXISTS public.create_tenant_integration(uuid,text,text,text,text,text,jsonb,integer,uuid)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.update_tenant_integration(uuid,uuid,text,text,text,text,text,jsonb,integer)"
    )
