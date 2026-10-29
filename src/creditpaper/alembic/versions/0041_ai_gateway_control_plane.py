"""Tenant-scoped integration and AI gateway control-plane data."""

from alembic import op

revision = "0041_ai_gateway_control_plane"
down_revision = "0040_tenant_llm_byok"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tenant_llm_providers
            ADD COLUMN fallback_model text,
            ADD COLUMN allowed_models text[] NOT NULL DEFAULT '{}',
            ADD COLUMN daily_token_limit bigint CHECK (daily_token_limit IS NULL OR daily_token_limit > 0),
            ADD COLUMN monthly_cost_limit_usd numeric(14,6)
                CHECK (monthly_cost_limit_usd IS NULL OR monthly_cost_limit_usd > 0),
            ADD COLUMN max_output_tokens integer NOT NULL DEFAULT 8192
                CHECK (max_output_tokens BETWEEN 1 AND 100000);

        CREATE TABLE tenant_integrations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
            provider text NOT NULL CHECK (length(btrim(provider)) BETWEEN 1 AND 80),
            base_url text NOT NULL,
            auth_type text NOT NULL CHECK (auth_type IN ('api_key','bearer','oauth_client_credentials','webhook')),
            encrypted_credentials text NOT NULL,
            config jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(config) = 'object'),
            rate_limit_per_minute integer NOT NULL DEFAULT 60 CHECK (rate_limit_per_minute BETWEEN 1 AND 6000),
            enabled boolean NOT NULL DEFAULT true,
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (tenant_id, name),
            UNIQUE (tenant_id, id)
        );
        CREATE INDEX ix_tenant_integrations_tenant ON tenant_integrations (tenant_id, created_at DESC);
        ALTER TABLE tenant_integrations ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_integrations FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON tenant_integrations
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

        CREATE TABLE tenant_integration_rate_windows (
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            integration_id uuid NOT NULL,
            window_start timestamptz NOT NULL,
            request_count integer NOT NULL DEFAULT 0 CHECK (request_count >= 0),
            PRIMARY KEY (tenant_id, integration_id, window_start),
            FOREIGN KEY (tenant_id, integration_id)
                REFERENCES tenant_integrations(tenant_id, id) ON DELETE CASCADE
        );
        ALTER TABLE tenant_integration_rate_windows ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_integration_rate_windows FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON tenant_integration_rate_windows
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

        CREATE TABLE tenant_integration_webhook_events (
            tenant_id uuid NOT NULL,
            integration_id uuid NOT NULL,
            event_id text NOT NULL CHECK (length(event_id) BETWEEN 1 AND 200),
            payload_sha256 text NOT NULL CHECK (length(payload_sha256) = 64),
            received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, integration_id, event_id),
            FOREIGN KEY (tenant_id, integration_id)
                REFERENCES tenant_integrations(tenant_id, id) ON DELETE CASCADE
        );
        ALTER TABLE tenant_integration_webhook_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_integration_webhook_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON tenant_integration_webhook_events
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

        CREATE TABLE tenant_mcp_servers (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
            endpoint text NOT NULL,
            auth_type text NOT NULL DEFAULT 'none' CHECK (auth_type IN ('none','bearer','api_key')),
            encrypted_credentials text,
            allowed_tools jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(allowed_tools) = 'array'),
            allowed_resources jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(allowed_resources) = 'array'),
            approved boolean NOT NULL DEFAULT false,
            enabled boolean NOT NULL DEFAULT true,
            created_by uuid,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (tenant_id, name),
            CHECK ((auth_type = 'none') = (encrypted_credentials IS NULL))
        );
        CREATE INDEX ix_tenant_mcp_servers_tenant ON tenant_mcp_servers (tenant_id, created_at DESC);
        ALTER TABLE tenant_mcp_servers ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_mcp_servers FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON tenant_mcp_servers
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

        CREATE TABLE tenant_integration_audit_events (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            actor_id uuid,
            actor_service text,
            resource_type text NOT NULL CHECK (resource_type IN ('integration','mcp_server','ai_gateway')),
            resource_id uuid,
            action text NOT NULL CHECK (length(action) BETWEEN 1 AND 80),
            details jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object'),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        CREATE INDEX ix_tenant_integration_audit_tenant_created
            ON tenant_integration_audit_events (tenant_id, created_at DESC);
        ALTER TABLE tenant_integration_audit_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_integration_audit_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON tenant_integration_audit_events
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

        REVOKE ALL ON tenant_integrations, tenant_integration_rate_windows,
            tenant_integration_webhook_events, tenant_mcp_servers, tenant_integration_audit_events FROM PUBLIC;
        REVOKE ALL ON SEQUENCE tenant_integration_audit_events_id_seq FROM PUBLIC;
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON tenant_integrations, tenant_integration_rate_windows,
                    tenant_integration_webhook_events, tenant_mcp_servers, tenant_integration_audit_events FROM anon;
                REVOKE ALL ON SEQUENCE tenant_integration_audit_events_id_seq FROM anon;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON tenant_integrations, tenant_integration_rate_windows,
                    tenant_integration_webhook_events, tenant_mcp_servers, tenant_integration_audit_events FROM authenticated;
                REVOKE ALL ON SEQUENCE tenant_integration_audit_events_id_seq FROM authenticated;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_integrations,
                    tenant_integration_rate_windows, tenant_integration_webhook_events,
                    tenant_mcp_servers TO creditpaper_runtime;
                GRANT SELECT, INSERT ON tenant_integration_audit_events TO creditpaper_runtime;
                GRANT USAGE, SELECT ON SEQUENCE tenant_integration_audit_events_id_seq TO creditpaper_runtime;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS tenant_integration_audit_events;
        DROP TABLE IF EXISTS tenant_mcp_servers;
        DROP TABLE IF EXISTS tenant_integration_webhook_events;
        DROP TABLE IF EXISTS tenant_integration_rate_windows;
        DROP TABLE IF EXISTS tenant_integrations;
        ALTER TABLE tenant_llm_providers
            DROP COLUMN IF EXISTS fallback_model,
            DROP COLUMN IF EXISTS allowed_models,
            DROP COLUMN IF EXISTS daily_token_limit,
            DROP COLUMN IF EXISTS monthly_cost_limit_usd,
            DROP COLUMN IF EXISTS max_output_tokens;
        """
    )
