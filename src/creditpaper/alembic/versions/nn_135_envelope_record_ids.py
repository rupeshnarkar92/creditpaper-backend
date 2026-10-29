"""Allow callers to bind new credential envelopes to stable record UUIDs."""

from alembic import op

revision = "nn_135_envelope_record_ids"
down_revision = "nn_134_complete_tenant_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION public.create_tenant_integration(
      p_id uuid,p_tenant_id uuid,p_name text,p_provider text,p_base_url text,p_auth_type text,
      p_encrypted_credentials text,p_config jsonb,p_rate_limit integer,p_created_by uuid
    ) RETURNS uuid LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      INSERT INTO public.tenant_integrations
        (id,tenant_id,name,provider,base_url,auth_type,encrypted_credentials,config,rate_limit_per_minute,created_by)
      VALUES(p_id,p_tenant_id,p_name,p_provider,p_base_url,p_auth_type,p_encrypted_credentials,p_config,p_rate_limit,p_created_by)
      RETURNING id
    $$;
    COMMENT ON FUNCTION public.create_tenant_integration(uuid,uuid,text,text,text,text,text,jsonb,integer,uuid) IS
      'Creates a tenant integration with a caller-generated stable UUID so credential AAD can bind to that record; returns the UUID.';
    REVOKE ALL ON FUNCTION public.create_tenant_integration(uuid,uuid,text,text,text,text,text,jsonb,integer,uuid) FROM PUBLIC;

    CREATE OR REPLACE FUNCTION public.create_tenant_mcp_server(
      p_id uuid,p_tenant_id uuid,p_name text,p_endpoint text,p_auth_type text,p_encrypted_credentials text,
      p_allowed_tools jsonb,p_allowed_resources jsonb,p_created_by uuid
    ) RETURNS uuid LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
      INSERT INTO public.tenant_mcp_servers
        (id,tenant_id,name,endpoint,auth_type,encrypted_credentials,allowed_tools,allowed_resources,created_by)
      VALUES(p_id,p_tenant_id,p_name,p_endpoint,p_auth_type,p_encrypted_credentials,p_allowed_tools,p_allowed_resources,p_created_by)
      RETURNING id
    $$;
    COMMENT ON FUNCTION public.create_tenant_mcp_server(uuid,uuid,text,text,text,text,jsonb,jsonb,uuid) IS
      'Creates a tenant MCP server with a caller-generated stable UUID so credential AAD can bind to that record; returns the UUID.';
    REVOKE ALL ON FUNCTION public.create_tenant_mcp_server(uuid,uuid,text,text,text,text,jsonb,jsonb,uuid) FROM PUBLIC;

    DO $$
    DECLARE role_name text; signature text;
    BEGIN
      FOREACH signature IN ARRAY ARRAY[
        'public.create_tenant_integration(uuid,uuid,text,text,text,text,text,jsonb,integer,uuid)',
        'public.create_tenant_mcp_server(uuid,uuid,text,text,text,text,jsonb,jsonb,uuid)'
      ] LOOP
        FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %I', signature, role_name);
          END IF;
        END LOOP;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
          EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO creditpaper_runtime', signature);
        END IF;
      END LOOP;
    END $$;
    """)


def downgrade() -> None:
    # Keep the record-bound routines; removing them would force weaker writes.
    pass
