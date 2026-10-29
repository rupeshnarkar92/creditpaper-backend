"""Immutable assertions, bitemporal resolutions, identity and retrieval.

Revision ID: 0012_temporal_ledger
Revises: 8de6e6b34d56
"""

from alembic import op

revision = "0012_temporal_ledger"
down_revision = "8de6e6b34d56"
branch_labels = None
depends_on = None

TABLES = (
    "ledger_assertions",
    "ledger_resolutions",
    "ledger_supersessions",
    "ledger_reviews",
    "ledger_approvals",
    "ledger_entities",
    "ledger_identifiers",
    "ledger_match_proposals",
    "ledger_entity_links",
    "ledger_edges",
    "ledger_edge_retractions",
    "ledger_chunks",
    "ledger_embeddings",
    "ledger_outbox",
)


def upgrade() -> None:
    op.execute("ALTER TABLE deals ADD CONSTRAINT uq_deals_tenant_id UNIQUE (tenant_id, id)")
    op.execute("ALTER TABLE deals ADD COLUMN ledger_revision bigint NOT NULL DEFAULT 0")
    op.execute("""
        ALTER TABLE deal_document_versions ADD CONSTRAINT uq_versions_tenant_deal_id
        UNIQUE (tenant_id, deal_id, id);
        CREATE FUNCTION ledger_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'canonical records are append-only' USING ERRCODE='55000'; END $$;

        CREATE TABLE ledger_assertions (
            tenant_id uuid NOT NULL, deal_id text NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(),
            field_path text NOT NULL CHECK (length(field_path) BETWEEN 1 AND 512),
            fact jsonb NOT NULL CHECK (jsonb_typeof(fact) = 'object' AND fact ? 'provenance'),
            valid_from timestamptz, valid_to timestamptz,
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            source_version_id uuid, source_system text,
            run_id uuid NOT NULL, fingerprint text NOT NULL,
            disposition text NOT NULL CHECK (disposition IN ('accepted','review','rejected','legacy')),
            PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id, deal_id, fingerprint),
            UNIQUE (tenant_id, deal_id, id),
            FOREIGN KEY (tenant_id, deal_id) REFERENCES deals(tenant_id,id),
            FOREIGN KEY (tenant_id, deal_id, source_version_id)
                REFERENCES deal_document_versions(tenant_id,deal_id,id),
            CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from),
            CHECK (source_version_id IS NOT NULL OR length(source_system) > 0)
        );
        CREATE TABLE ledger_resolutions (
            tenant_id uuid NOT NULL, deal_id text NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(),
            field_path text NOT NULL, assertion_id uuid NOT NULL,
            valid_from timestamptz, valid_to timestamptz,
            valid_period tstzrange GENERATED ALWAYS AS (tstzrange(valid_from,valid_to,'[)')) STORED,
            recorded_at timestamptz NOT NULL, actor_id uuid, reason text NOT NULL,
            PRIMARY KEY (tenant_id,id), UNIQUE(tenant_id,deal_id,id),
            FOREIGN KEY (tenant_id,deal_id,assertion_id) REFERENCES ledger_assertions(tenant_id,deal_id,id),
            CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from)
        );
        CREATE TABLE ledger_supersessions (
            tenant_id uuid NOT NULL, deal_id text NOT NULL, resolution_id uuid NOT NULL,
            recorded_at timestamptz NOT NULL, actor_id uuid, reason text NOT NULL,
            PRIMARY KEY(tenant_id,resolution_id),
            FOREIGN KEY(tenant_id,deal_id,resolution_id) REFERENCES ledger_resolutions(tenant_id,deal_id,id)
        );
        CREATE TABLE ledger_reviews (
            tenant_id uuid NOT NULL, deal_id text NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(),
            assertion_id uuid NOT NULL, action text NOT NULL CHECK(action IN ('accept','reject')),
            actor_id uuid NOT NULL, reason text NOT NULL, recorded_at timestamptz NOT NULL,
            PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,assertion_id),
            FOREIGN KEY(tenant_id,deal_id,assertion_id) REFERENCES ledger_assertions(tenant_id,deal_id,id)
        );
        CREATE TABLE ledger_approvals (
            tenant_id uuid NOT NULL, deal_id text NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(),
            actor_id uuid NOT NULL, valid_at timestamptz NOT NULL, known_at timestamptz NOT NULL,
            ledger_revision bigint NOT NULL, manifest jsonb NOT NULL, digest text NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,deal_id) REFERENCES deals(tenant_id,id)
        );
        CREATE TABLE ledger_entities (
            tenant_id uuid NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(), name text NOT NULL,
            kind text NOT NULL CHECK(kind IN ('company','trust','person','partnership','facility','asset')),
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(tenant_id,id), CHECK(length(name) BETWEEN 1 AND 300)
        );
        CREATE TABLE ledger_identifiers (
            tenant_id uuid NOT NULL, namespace text NOT NULL, value text NOT NULL, entity_id uuid NOT NULL,
            source_deal_id text NOT NULL, source_assertion_id uuid NOT NULL, actor_id uuid NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(tenant_id,namespace,value),
            FOREIGN KEY(tenant_id,entity_id) REFERENCES ledger_entities(tenant_id,id),
            FOREIGN KEY(tenant_id,source_deal_id,source_assertion_id)
                REFERENCES ledger_assertions(tenant_id,deal_id,id)
        );
        CREATE TABLE ledger_match_proposals (
            tenant_id uuid NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(),
            left_id uuid NOT NULL, right_id uuid NOT NULL, score double precision NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,left_id,right_id),
            FOREIGN KEY(tenant_id,left_id) REFERENCES ledger_entities(tenant_id,id),
            FOREIGN KEY(tenant_id,right_id) REFERENCES ledger_entities(tenant_id,id),
            CHECK(left_id <> right_id), CHECK(score BETWEEN 0 AND 1)
        );
        CREATE TABLE ledger_entity_links (
            tenant_id uuid NOT NULL, proposal_id uuid NOT NULL, actor_id uuid NOT NULL,
            accepted boolean NOT NULL, reason text NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(tenant_id,proposal_id),
            FOREIGN KEY(tenant_id,proposal_id) REFERENCES ledger_match_proposals(tenant_id,id)
        );
        CREATE TABLE ledger_edges (
            tenant_id uuid NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(),
            source_id uuid NOT NULL, target_id uuid NOT NULL, kind text NOT NULL,
            deal_id text NOT NULL, assertion_id uuid NOT NULL,
            valid_from timestamptz NOT NULL, valid_to timestamptz,
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            actor_id uuid NOT NULL, PRIMARY KEY(tenant_id,id),
            FOREIGN KEY(tenant_id,source_id) REFERENCES ledger_entities(tenant_id,id),
            FOREIGN KEY(tenant_id,target_id) REFERENCES ledger_entities(tenant_id,id),
            FOREIGN KEY(tenant_id,deal_id,assertion_id) REFERENCES ledger_assertions(tenant_id,deal_id,id),
            CHECK(source_id <> target_id),
            CHECK(kind IN ('controls','borrows','guarantees','secured_by','exposure')),
            CHECK(valid_to IS NULL OR valid_to > valid_from)
        );
        CREATE TABLE ledger_edge_retractions (
            tenant_id uuid NOT NULL, edge_id uuid NOT NULL, actor_id uuid NOT NULL, reason text NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(tenant_id,edge_id),
            FOREIGN KEY(tenant_id,edge_id) REFERENCES ledger_edges(tenant_id,id)
        );
        CREATE TABLE ledger_chunks (
            tenant_id uuid NOT NULL, deal_id text NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(),
            version_id uuid NOT NULL, page integer NOT NULL CHECK(page > 0), location text NOT NULL,
            chunk_index integer NOT NULL CHECK(chunk_index >= 0), content text NOT NULL,
            content_hash text NOT NULL, recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            search tsvector GENERATED ALWAYS AS (to_tsvector('english',content)) STORED,
            PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,version_id,page,chunk_index,content_hash),
            FOREIGN KEY(tenant_id,deal_id,version_id) REFERENCES deal_document_versions(tenant_id,deal_id,id),
            CHECK(length(content) BETWEEN 1 AND 10000)
        );
        CREATE TABLE ledger_embeddings (
            tenant_id uuid NOT NULL, chunk_id uuid NOT NULL, model text NOT NULL,
            embedding double precision[] NOT NULL, dimensions integer NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(tenant_id,chunk_id,model),
            FOREIGN KEY(tenant_id,chunk_id) REFERENCES ledger_chunks(tenant_id,id),
            CHECK(dimensions BETWEEN 1 AND 4096), CHECK(cardinality(embedding)=dimensions)
        );
        CREATE TABLE ledger_outbox (
            tenant_id uuid NOT NULL, id bigint GENERATED ALWAYS AS IDENTITY,
            deal_id text NOT NULL, event_type text NOT NULL, payload jsonb NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,deal_id) REFERENCES deals(tenant_id,id)
        );
        CREATE VIEW ledger_fact_history WITH (security_invoker=true) AS
            SELECT r.*, a.fact, a.source_version_id, a.source_system,
                   (a.valid_from IS NULL) AS effective_date_unknown,
                   s.recorded_at AS superseded_at
            FROM ledger_resolutions r JOIN ledger_assertions a
                ON a.tenant_id=r.tenant_id AND a.id=r.assertion_id
            LEFT JOIN ledger_supersessions s ON s.tenant_id=r.tenant_id AND s.resolution_id=r.id;
        CREATE INDEX ix_assertions_deal_path ON ledger_assertions(tenant_id,deal_id,field_path,recorded_at);
        CREATE INDEX ix_resolutions_deal_path ON ledger_resolutions(tenant_id,deal_id,field_path,recorded_at);
        CREATE INDEX ix_resolutions_period ON ledger_resolutions USING gist(valid_period);
        CREATE INDEX ix_edges_source ON ledger_edges(tenant_id,source_id,recorded_at);
        CREATE INDEX ix_edges_target ON ledger_edges(tenant_id,target_id,recorded_at);
        CREATE INDEX ix_chunks_deal ON ledger_chunks(tenant_id,deal_id,version_id);
        CREATE INDEX ix_chunks_search ON ledger_chunks USING gin(search);
        CREATE INDEX ix_approvals_deal ON ledger_approvals(tenant_id,deal_id,recorded_at);
        CREATE INDEX ix_outbox_deal ON ledger_outbox(tenant_id,deal_id,id);
        CREATE INDEX ix_entities_name ON ledger_entities(tenant_id,lower(name));
        ALTER TABLE fact_review_queue ADD COLUMN assertion_id uuid;
        ALTER TABLE fact_review_queue ADD CONSTRAINT fk_review_assertion
            FOREIGN KEY(tenant_id,deal_id,assertion_id) REFERENCES ledger_assertions(tenant_id,deal_id,id);
        CREATE UNIQUE INDEX uq_review_assertion ON fact_review_queue(tenant_id,assertion_id);
    """)
    for table in TABLES:
        op.execute(f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id',true),'')::uuid);
            CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION ledger_immutable();
            REVOKE ALL ON {table} FROM PUBLIC;
        """)
    op.execute("REVOKE ALL ON ledger_fact_history FROM PUBLIC")


def downgrade() -> None:
    # Destructive rollback requires an explicit operator export/recovery plan.
    raise RuntimeError("Temporal ledger cannot be dropped by downgrade; restore a verified backup instead")
