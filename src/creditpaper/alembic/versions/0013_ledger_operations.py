"""Durable leased processing jobs and canonical integrity constraints."""

from alembic import op

revision = "0013_ledger_operations"
down_revision = "0012_temporal_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE ledger_assertions ADD CONSTRAINT ck_assertion_source
          CHECK(source_version_id IS NOT NULL OR (source_system IS NOT NULL AND length(btrim(source_system))>0));
        ALTER TABLE ledger_assertions ADD CONSTRAINT ck_assertion_anchor
          CHECK(jsonb_typeof(fact->'provenance')='object'
            AND length(fact->'provenance'->>'document_id')>0
            AND length(fact->'provenance'->>'location')>0);
        CREATE FUNCTION ledger_resolution_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE p text; BEGIN
          PERFORM 1 FROM deals WHERE tenant_id=NEW.tenant_id AND id=NEW.deal_id FOR UPDATE;
          SELECT field_path INTO p FROM ledger_assertions WHERE tenant_id=NEW.tenant_id AND id=NEW.assertion_id;
          IF p IS DISTINCT FROM NEW.field_path THEN
            RAISE EXCEPTION 'resolution field does not match assertion' USING ERRCODE='23514';
          END IF;
          IF EXISTS(SELECT 1 FROM ledger_resolutions r WHERE r.tenant_id=NEW.tenant_id
            AND r.deal_id=NEW.deal_id AND r.field_path=NEW.field_path
            AND r.valid_period && tstzrange(NEW.valid_from,NEW.valid_to,'[)')
            AND NOT EXISTS(SELECT 1 FROM ledger_supersessions s WHERE s.tenant_id=r.tenant_id AND s.resolution_id=r.id))
          THEN RAISE EXCEPTION 'overlapping current fact resolutions' USING ERRCODE='23514'; END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER resolution_integrity BEFORE INSERT ON ledger_resolutions
          FOR EACH ROW EXECUTE FUNCTION ledger_resolution_guard();
        CREATE FUNCTION ledger_supersession_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NOT EXISTS(SELECT 1 FROM ledger_resolutions r WHERE r.tenant_id=NEW.tenant_id
              AND r.id=NEW.resolution_id AND r.recorded_at<=NEW.recorded_at)
          THEN RAISE EXCEPTION 'supersession predates resolution' USING ERRCODE='23514'; END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER supersession_integrity BEFORE INSERT ON ledger_supersessions
          FOR EACH ROW EXECUTE FUNCTION ledger_supersession_guard();
        CREATE TABLE processing_jobs (
          tenant_id uuid NOT NULL, deal_id text NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(),
          kind text NOT NULL CHECK(kind IN ('generation','extraction','document')),
          resource_id text NOT NULL, payload jsonb NOT NULL, result jsonb NOT NULL DEFAULT '{}',
          status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','complete','failed')),
          attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
          lease_token uuid, lease_until timestamptz,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,deal_id) REFERENCES deals(tenant_id,id),
          CHECK(status<>'running' OR (lease_token IS NOT NULL AND lease_until IS NOT NULL))
        );
        CREATE UNIQUE INDEX uq_processing_active ON processing_jobs(tenant_id,deal_id,kind,resource_id)
          WHERE status IN ('pending','running');
        CREATE INDEX ix_processing_claim ON processing_jobs(tenant_id,status,created_at);
        ALTER TABLE processing_jobs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE processing_jobs FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON processing_jobs
          USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
        REVOKE ALL ON processing_jobs FROM PUBLIC;
    """)
    # Portable Postgres works without vector binaries. Hosted Postgres can enable
    # this extension before migration to use indexed vector search.
    # The check runs inside the database so the migration also works in offline (--sql) mode.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
            ALTER TABLE ledger_embeddings
              ADD COLUMN embedding_vector vector GENERATED ALWAYS AS (embedding::vector) STORED;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Operational ledger history requires a verified backup restore, not a destructive downgrade"
    )
