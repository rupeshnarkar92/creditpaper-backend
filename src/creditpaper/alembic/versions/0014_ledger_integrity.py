"""Strict evidence anchors and reversible human identity decisions."""

from alembic import op

revision = "0014_ledger_integrity"
down_revision = "0013_ledger_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE ledger_assertions DROP CONSTRAINT ck_assertion_anchor;
        ALTER TABLE ledger_assertions ADD CONSTRAINT ck_assertion_anchor CHECK(COALESCE(
            jsonb_typeof(fact->'provenance')='object'
            AND length(btrim(fact->'provenance'->>'document_id'))>0
            AND length(btrim(fact->'provenance'->>'location'))>0
            AND fact->'provenance'->>'method' IN
                ('llm_extraction','mapped_reader','derived','human_entry'), false));
        CREATE OR REPLACE VIEW ledger_fact_history WITH (security_invoker=true) AS
            SELECT r.*, a.fact, a.source_version_id, a.source_system,
                   (r.valid_from IS NULL) AS effective_date_unknown,
                   s.recorded_at AS superseded_at
            FROM ledger_resolutions r
            JOIN ledger_assertions a ON a.tenant_id=r.tenant_id AND a.id=r.assertion_id
            LEFT JOIN ledger_supersessions s ON s.tenant_id=r.tenant_id AND s.resolution_id=r.id;
        CREATE TABLE ledger_entity_link_retractions (
            tenant_id uuid NOT NULL, proposal_id uuid NOT NULL, actor_id uuid NOT NULL,
            reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 2000),
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(tenant_id,proposal_id),
            FOREIGN KEY(tenant_id,proposal_id) REFERENCES ledger_entity_links(tenant_id,proposal_id)
        );
        ALTER TABLE ledger_entity_link_retractions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE ledger_entity_link_retractions FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON ledger_entity_link_retractions
            USING(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid)
            WITH CHECK(tenant_id=NULLIF(current_setting('app.tenant_id',true),'')::uuid);
        CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON ledger_entity_link_retractions
            FOR EACH ROW EXECUTE FUNCTION ledger_immutable();
        REVOKE ALL ON ledger_entity_link_retractions FROM PUBLIC;
        CREATE INDEX ix_ledger_outbox_history ON ledger_outbox(tenant_id,deal_id,recorded_at);
        CREATE INDEX ix_ledger_identity_candidates ON ledger_entities(tenant_id,kind,lower(name) text_pattern_ops);
    """)


def downgrade() -> None:
    raise RuntimeError("Immutable identity history requires a verified restore, not destructive rollback")
