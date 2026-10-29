"""deal recommendations: the human-signed Section 9 recommendation (D8).

Revision ID: 0023_deal_recommendations
Revises: 0022_job_tenants_role_default

Ruled 2026-09-15: the Section 9 recommendation is a dedicated field only a person
writes, signed by a named user; approval requires it. Every save is a new signed
version: the table is append-only (the ledger's `ledger_immutable` trigger refuses
UPDATE and DELETE), tenant-isolated by forced RLS on `app.tenant_id`, and the runtime
role may only read and insert. Generation never writes here.

ADDITIVE ONLY. Offline-safe: role checks run inside the database. Downgrade drops the
table (and with it every signed recommendation), so run it only on a database whose
recommendations have been exported or are disposable.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023_deal_recommendations"
down_revision: str | None = "0022_job_tenants_role_default"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE deal_recommendations (
            tenant_id uuid NOT NULL,
            deal_id text NOT NULL,
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            body text NOT NULL CHECK (length(btrim(body)) BETWEEN 1 AND 20000),
            author_id uuid NOT NULL,
            signed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            ledger_revision bigint,
            PRIMARY KEY (tenant_id, id),
            FOREIGN KEY (tenant_id, deal_id) REFERENCES deals (tenant_id, id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_deal_recommendations_deal_signed "
        "ON deal_recommendations (tenant_id, deal_id, signed_at DESC)"
    )
    op.execute(
        """
        ALTER TABLE deal_recommendations ENABLE ROW LEVEL SECURITY;
        ALTER TABLE deal_recommendations FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON deal_recommendations
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
        CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON deal_recommendations
            FOR EACH ROW EXECUTE FUNCTION ledger_immutable();
        REVOKE ALL ON deal_recommendations FROM PUBLIC
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON deal_recommendations FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON deal_recommendations FROM authenticated;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
            REVOKE ALL ON deal_recommendations FROM creditpaper_runtime;
            GRANT SELECT, INSERT ON deal_recommendations TO creditpaper_runtime;
          END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS deal_recommendations")
