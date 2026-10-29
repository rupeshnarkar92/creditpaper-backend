"""Tenant-owned BYOK provider credentials and per-organization usage telemetry.

Secrets remain ciphertext in Postgres; only the trusted runtime role can access
these tables, and forced RLS binds every read/write to app.tenant_id.
"""

from alembic import op

revision = "0040_tenant_llm_byok"
down_revision = "0039_auth_app_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tenant_llm_providers (
            tenant_id uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            provider text NOT NULL CHECK (provider IN ('openai','anthropic','google','compatible')),
            model text NOT NULL CHECK (length(btrim(model)) BETWEEN 1 AND 160),
            encrypted_api_key text NOT NULL,
            base_url text,
            input_price_per_million_usd numeric(14,6),
            output_price_per_million_usd numeric(14,6),
            enabled boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            CHECK (input_price_per_million_usd IS NULL OR input_price_per_million_usd >= 0),
            CHECK (output_price_per_million_usd IS NULL OR output_price_per_million_usd >= 0),
            CHECK ((input_price_per_million_usd IS NULL) = (output_price_per_million_usd IS NULL)),
            CHECK (provider = 'compatible' OR base_url IS NULL)
        );

        CREATE TABLE tenant_llm_usage_events (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            provider text NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
            model text NOT NULL CHECK (length(model) BETWEEN 1 AND 160),
            tool text NOT NULL CHECK (length(tool) BETWEEN 1 AND 200),
            input_tokens bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
            output_tokens bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
            cache_write_tokens bigint NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
            cache_read_tokens bigint NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
            cost_usd numeric(18,10) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
            latency_s double precision NOT NULL DEFAULT 0 CHECK (latency_s >= 0),
            unpriced boolean NOT NULL DEFAULT false,
            truncated boolean NOT NULL DEFAULT false,
            failed boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        CREATE INDEX ix_tenant_llm_usage_tenant_created
            ON tenant_llm_usage_events (tenant_id, created_at DESC);
        CREATE INDEX ix_tenant_llm_usage_tenant_provider_model
            ON tenant_llm_usage_events (tenant_id, provider, model, created_at DESC);

        ALTER TABLE tenant_llm_providers ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_llm_providers FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON tenant_llm_providers
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
        ALTER TABLE tenant_llm_usage_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE tenant_llm_usage_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON tenant_llm_usage_events
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

        REVOKE ALL ON tenant_llm_providers, tenant_llm_usage_events FROM PUBLIC;
        REVOKE ALL ON SEQUENCE tenant_llm_usage_events_id_seq FROM PUBLIC;
        DO $$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format('REVOKE ALL ON tenant_llm_providers, tenant_llm_usage_events FROM %I', role_name);
                    EXECUTE format('REVOKE ALL ON SEQUENCE tenant_llm_usage_events_id_seq FROM %I', role_name);
                END IF;
            END LOOP;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_llm_providers TO creditpaper_runtime;
                GRANT SELECT, INSERT ON tenant_llm_usage_events TO creditpaper_runtime;
                GRANT USAGE, SELECT ON SEQUENCE tenant_llm_usage_events_id_seq TO creditpaper_runtime;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tenant_llm_usage_events")
    op.execute("DROP TABLE IF EXISTS tenant_llm_providers")
