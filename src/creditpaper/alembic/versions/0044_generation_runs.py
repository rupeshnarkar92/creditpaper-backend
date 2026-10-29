"""Generation runs, their stage outputs and their spend (spec §12), tenant-isolated.

A run pins one ledger revision; each stage's output is stored under a cache key
so a crash resumes at the failed stage and an unchanged stage is never re-run.
Spend is recorded per call, failed calls included, so the per-run cost ceiling
survives a crash.

Every reference carries the tenant: foreign keys and unique constraints are
checked outside row-level security, so a single-column reference would let one
tenant's row point at another's (or pre-empt its unique slot). The house
convention since 0012 is `FOREIGN KEY (tenant_id, x) REFERENCES t(tenant_id, id)`.
"""

from alembic import op

revision = "0044_generation_runs"
down_revision = "eeba3e2314cf"
branch_labels = None
depends_on = None

_TABLES = ("generation_runs", "generation_stage_outputs", "generation_spend")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE generation_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL,
            deal_id text NOT NULL,
            job_id uuid,
            ledger_revision bigint NOT NULL,
            fact_digest text NOT NULL,
            harness_version text NOT NULL,
            manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
            verdict text CHECK (verdict IN ('complete','complete_with_flags','degraded',
                                            'fallback_delivered','unverified')),
            cost_usd numeric(14,6) NOT NULL DEFAULT 0,
            started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            finished_at timestamptz,
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, deal_id) REFERENCES deals(tenant_id, id) ON DELETE CASCADE
        );
        CREATE INDEX ix_generation_runs_deal ON generation_runs (tenant_id, deal_id, started_at DESC);

        CREATE TABLE generation_stage_outputs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL,
            run_id uuid NOT NULL,
            stage text NOT NULL,
            unit text NOT NULL DEFAULT '',
            cache_key text NOT NULL,
            output jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (tenant_id, run_id, stage, unit),
            FOREIGN KEY (tenant_id, run_id) REFERENCES generation_runs(tenant_id, id) ON DELETE CASCADE
        );
        CREATE INDEX ix_generation_stage_outputs_key
            ON generation_stage_outputs (tenant_id, cache_key, created_at DESC);

        CREATE TABLE generation_spend (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL,
            run_id uuid NOT NULL,
            stage text NOT NULL,
            unit text NOT NULL DEFAULT '',
            cost_usd numeric(14,6) NOT NULL,
            failed boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            FOREIGN KEY (tenant_id, run_id) REFERENCES generation_runs(tenant_id, id) ON DELETE CASCADE
        );
        CREATE INDEX ix_generation_spend_run ON generation_spend (tenant_id, run_id);
        """
    )
    for table in _TABLES:
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY {table}_tenant ON {table}
                USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
            REVOKE ALL ON {table} FROM PUBLIC;
            """
        )
    op.execute(
        """
        DO $$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format(
                        'REVOKE ALL ON generation_runs, generation_stage_outputs, generation_spend FROM %I',
                        role_name);
                END IF;
            END LOOP;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
                -- Runs are finished in place; stage outputs and spend are written once.
                GRANT SELECT, INSERT, UPDATE ON generation_runs TO creditpaper_runtime;
                GRANT SELECT, INSERT ON generation_stage_outputs, generation_spend TO creditpaper_runtime;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS generation_spend; DROP TABLE IF EXISTS generation_stage_outputs; "
        "DROP TABLE IF EXISTS generation_runs;"
    )
