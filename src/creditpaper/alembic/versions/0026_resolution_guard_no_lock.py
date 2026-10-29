"""Remove redundant FOR UPDATE from ledger_resolution_guard trigger.

The trigger previously acquired a FOR UPDATE lock on the deals row before
allowing INSERTs into ledger_resolutions.  This is redundant — the caller
(e.g. commit_outputs) already holds the deal lock — and causes unnecessary
lock contention.  Replace the trigger function with one that validates
integrity without locking.
"""

from alembic import op

revision = "0026_resolution_guard_no_lock"
down_revision = "0025_paper_editor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ledger_resolution_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE p text; BEGIN
          -- Integrity check only — no FOR UPDATE (caller already holds the deal lock).
          SELECT field_path INTO p FROM ledger_assertions
            WHERE tenant_id=NEW.tenant_id AND id=NEW.assertion_id;
          IF p IS DISTINCT FROM NEW.field_path THEN
            RAISE EXCEPTION 'resolution field does not match assertion'
              USING ERRCODE='23514';
          END IF;
          IF EXISTS(
            SELECT 1 FROM ledger_resolutions r
            WHERE r.tenant_id=NEW.tenant_id AND r.deal_id=NEW.deal_id
              AND r.field_path=NEW.field_path
              AND r.valid_period && tstzrange(NEW.valid_from, NEW.valid_to, '[)')
              AND NOT EXISTS(
                SELECT 1 FROM ledger_supersessions s
                WHERE s.tenant_id=r.tenant_id AND s.resolution_id=r.id
              )
          ) THEN
            RAISE EXCEPTION 'overlapping current fact resolutions'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION ledger_resolution_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE p text; BEGIN
          PERFORM 1 FROM deals WHERE tenant_id=NEW.tenant_id AND id=NEW.deal_id FOR UPDATE;
          SELECT field_path INTO p FROM ledger_assertions
            WHERE tenant_id=NEW.tenant_id AND id=NEW.assertion_id;
          IF p IS DISTINCT FROM NEW.field_path THEN
            RAISE EXCEPTION 'resolution field does not match assertion'
              USING ERRCODE='23514';
          END IF;
          IF EXISTS(
            SELECT 1 FROM ledger_resolutions r
            WHERE r.tenant_id=NEW.tenant_id AND r.deal_id=NEW.deal_id
              AND r.field_path=NEW.field_path
              AND r.valid_period && tstzrange(NEW.valid_from, NEW.valid_to, '[)')
              AND NOT EXISTS(
                SELECT 1 FROM ledger_supersessions s
                WHERE s.tenant_id=r.tenant_id AND s.resolution_id=r.id
              )
          ) THEN
            RAISE EXCEPTION 'overlapping current fact resolutions'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$;
        """
    )
