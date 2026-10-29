"""Tenant-isolated ingestion cache, intake records and fact audit tables.

Revision ID: 0043_ingestion_data
Revises: 0042_merge_tenant_gateway

All additions descend from the active merged head. Runtime access is limited
by each store's mutation contract; this migration is additive and must not be
applied to a live database without the normal deployment review.
"""

from alembic import op

revision = "0043_ingestion_data"
down_revision = "0042_merge_tenant_gateway"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ingest_parse_artifacts (
            tenant_id uuid NOT NULL,
            content_hash varchar(64) NOT NULL,
            parser_version varchar(64) NOT NULL,
            route text NOT NULL,
            page_count integer NOT NULL CHECK (page_count >= 0),
            pages jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, content_hash, parser_version)
        );

        CREATE TABLE ingest_proposals (
            tenant_id uuid NOT NULL,
            deal_id text NOT NULL,
            content_hash varchar(64) NOT NULL,
            filename text NOT NULL,
            outcome varchar(32) NOT NULL,
            slot_key varchar(64),
            confidence double precision NOT NULL DEFAULT 0,
            evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
            needs_party_context boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, deal_id, content_hash)
        );

        CREATE TABLE ingest_confirmations (
            tenant_id uuid NOT NULL,
            deal_id text NOT NULL,
            content_hash varchar(64) NOT NULL,
            slot_key varchar(64) NOT NULL,
            confirmed_by text NOT NULL,
            confirmed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, deal_id, content_hash)
        );

        CREATE TABLE ingest_held_facts (
            tenant_id uuid NOT NULL,
            deal_id text NOT NULL,
            content_hash varchar(64) NOT NULL,
            proposed_slot_key varchar(64) NOT NULL,
            facts jsonb NOT NULL,
            coverage jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, deal_id, content_hash)
        );

        CREATE TABLE ingest_fact_cache (
            tenant_id uuid NOT NULL,
            deal_id varchar(128) NOT NULL,
            content_hash varchar(64) NOT NULL,
            prompt_version varchar(32) NOT NULL,
            model_id varchar(128) NOT NULL,
            facts jsonb NOT NULL,
            fact_count integer NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, deal_id, content_hash, prompt_version, model_id)
        );

        CREATE TABLE fact_labels (
            tenant_id uuid NOT NULL,
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            deal_id text NOT NULL,
            concept_path text NOT NULL CHECK (length(concept_path) BETWEEN 1 AND 500),
            assertion_id uuid,
            document_version_id uuid,
            page integer CHECK (page IS NULL OR page >= 1),
            quote text,
            extracted_value jsonb,
            final_value jsonb,
            action text NOT NULL CHECK (action IN ('accepted', 'corrected', 'other_source', 'set_aside')),
            reason_code text,
            verdict_at_decision text,
            verify_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
            extractor_version text,
            model_id text,
            prompt_version text,
            decided_by uuid,
            decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            source text NOT NULL CHECK (source IN ('analyst', 'seed')),
            PRIMARY KEY (tenant_id, id),
            CHECK (source = 'seed' OR decided_by IS NOT NULL)
        );
        CREATE INDEX ix_fact_labels_concept
            ON fact_labels (tenant_id, deal_id, concept_path, decided_at DESC);

        CREATE TABLE contradiction_register (
            tenant_id uuid NOT NULL,
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            deal_id text NOT NULL,
            concept_path text NOT NULL CHECK (length(concept_path) BETWEEN 1 AND 500),
            assertion_ids uuid[] NOT NULL CHECK (cardinality(assertion_ids) >= 2),
            diff_class text NOT NULL CHECK (diff_class IN
                ('contradiction', 'basis_difference', 'rounding', 'superseded', 'temporal')),
            material boolean NOT NULL,
            status text NOT NULL CHECK (status IN ('open', 'resolved', 'noted')),
            resolved_value jsonb,
            resolved_assertion_id uuid,
            resolved_by uuid,
            reason text,
            resolved_at timestamptz,
            registry_version text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, id),
            FOREIGN KEY (tenant_id, deal_id) REFERENCES deals (tenant_id, id) ON DELETE CASCADE,
            CHECK (status <> 'open' OR diff_class = 'contradiction'),
            CHECK (status <> 'resolved' OR (resolved_by IS NOT NULL AND resolved_at IS NOT NULL
                AND reason IS NOT NULL AND length(btrim(reason)) >= 1))
        );
        CREATE INDEX ix_contradiction_register_deal
            ON contradiction_register (tenant_id, deal_id, concept_path, status);

        ALTER TABLE processing_jobs DROP CONSTRAINT IF EXISTS processing_jobs_kind_check;
        ALTER TABLE processing_jobs ADD CONSTRAINT processing_jobs_kind_check
            CHECK (kind = ANY (ARRAY[
                'generation', 'extraction', 'document', 'parse', 'corpus_extraction',
                'ingest_window', 'ingest_speculate', 'ingest_propose'
            ]::text[]));

        CREATE OR REPLACE FUNCTION set_document_processing_status(
            p_tenant_id uuid,
            p_deal_id text,
            p_version_id uuid,
            p_status text,
            p_reason text DEFAULT NULL
        ) RETURNS boolean
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF p_status NOT IN ('pending', 'processing', 'ready', 'failed') THEN
                RAISE EXCEPTION 'invalid processing status' USING ERRCODE = '22023';
            END IF;
            UPDATE deal_document_versions
               SET processing_status = p_status,
                   processing_error = CASE WHEN p_reason IS NULL THEN NULL ELSE left(p_reason, 500) END
             WHERE tenant_id = p_tenant_id AND deal_id = p_deal_id AND id = p_version_id;
            RETURN FOUND;
        END;
        $$;

        CREATE OR REPLACE FUNCTION set_document_kind_if_unset(
            p_tenant_id uuid,
            p_deal_id text,
            p_document_id uuid,
            p_document_kind text
        ) RETURNS boolean
        LANGUAGE plpgsql
        AS $$
        BEGIN
            UPDATE deal_documents
               SET document_kind = p_document_kind
             WHERE tenant_id = p_tenant_id AND deal_id = p_deal_id
               AND id = p_document_id AND document_kind IS NULL;
            RETURN FOUND;
        END;
        $$;
        REVOKE ALL ON FUNCTION set_document_processing_status(uuid,text,uuid,text,text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION set_document_kind_if_unset(uuid,text,uuid,text) FROM PUBLIC;
        """
    )

    for table, policy in (
        ("ingest_parse_artifacts", "ingest_parse_artifacts_tenant"),
        ("ingest_proposals", "ingest_proposals_tenant"),
        ("ingest_confirmations", "ingest_confirmations_tenant"),
        ("ingest_held_facts", "ingest_held_facts_tenant"),
        ("ingest_fact_cache", "ingest_fact_cache_tenant"),
        ("fact_labels", "fact_labels_tenant"),
        ("contradiction_register", "contradiction_register_tenant"),
    ):
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY {policy} ON {table}
                USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
            REVOKE ALL ON {table} FROM PUBLIC;
            """
        )

    op.execute(
        "CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON fact_labels "
        "FOR EACH ROW EXECUTE FUNCTION ledger_immutable()"
    )
    op.execute("REVOKE ALL ON processing_jobs FROM PUBLIC")

    op.execute(
        """
        DO $$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    REVOKE ALL ON ingest_parse_artifacts, ingest_proposals, ingest_confirmations,
                        ingest_held_facts, ingest_fact_cache, fact_labels, contradiction_register
                        FROM PUBLIC;
                    EXECUTE format('REVOKE ALL ON ingest_parse_artifacts, ingest_proposals, '
                        'ingest_confirmations, ingest_held_facts, ingest_fact_cache, fact_labels, '
                        'contradiction_register, processing_jobs FROM %I', role_name);
                END IF;
            END LOOP;

            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
                REVOKE ALL ON ingest_parse_artifacts, ingest_proposals, ingest_confirmations,
                    ingest_held_facts, ingest_fact_cache, fact_labels, contradiction_register,
                    processing_jobs FROM creditpaper_runtime;
                GRANT SELECT, INSERT ON ingest_parse_artifacts, fact_labels TO creditpaper_runtime;
                -- Proposals and confirmations are upserted, never deleted. The
                -- contradiction register is resolvable but undeletable. Jobs
                -- may update leases/status but retain their audit history.
                GRANT SELECT, INSERT, UPDATE ON ingest_proposals, ingest_confirmations,
                    contradiction_register, processing_jobs TO creditpaper_runtime;
                GRANT SELECT, INSERT, UPDATE, DELETE ON ingest_held_facts TO creditpaper_runtime;
                -- Deliberate invalidation deletes one deal's cached draws; cache rows are
                -- never updated in place, and tenant RLS still applies to the delete.
                GRANT SELECT, INSERT, DELETE ON ingest_fact_cache TO creditpaper_runtime;
                GRANT EXECUTE ON FUNCTION set_document_processing_status(uuid,text,uuid,text,text),
                    set_document_kind_if_unset(uuid,text,uuid,text) TO creditpaper_runtime;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Ingestion records are tenant audit/cache data; restore a verified backup instead of downgrading"
    )
