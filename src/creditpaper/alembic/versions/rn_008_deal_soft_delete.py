"""Soft delete for deals: archive the row instead of destroying the record.

Revision ID: rn_008_deal_soft_delete
Revises: rn_007_mfa_policy_admin

``DELETE /api/deals/{id}`` issued a bare ``DELETE FROM deals``. Two problems, both
found by reading the live foreign keys rather than the route:

- ``deal_audit_events``, ``deal_documents``, ``document_slots``,
  ``deal_state_transitions`` and ``deal_workspaces`` reference deals with
  ``ON DELETE CASCADE``, so a delete took the deal's entire audit trail, document
  set and slot board with it. The gap was not "no audit event written"; it was
  "every audit event erased".
- ``deal_paper_sections``, ``deal_recommendations``, ``ledger_approvals``,
  ``ledger_assertions``, ``ledger_outbox`` and ``processing_jobs`` use the default
  ``NO ACTION``, so a delete on a deal holding any of those raised a foreign key
  violation instead of succeeding.

``deals.deleted_at`` is NULL while the deal is live. The row, its documents, its
ledger and its audit events all survive; only the reads stop returning it.

Two stored procedures are patched in place with the ``pg_get_functiondef`` +
``replace`` + ``EXECUTE`` technique 0033/0034/0035 already established, so their
bodies are not duplicated here. Each patch raises when its anchor string is absent,
so a definition that drifts fails the migration loudly rather than silently
shipping a filter that never landed.

- ``get_deals_list`` - the deals board, the lists and the home dashboard's
  ``v_deals`` (``get_home_dashboard`` calls it).
- ``get_workspace_payload`` - the workspace's gating read. ``get_workspace_page``
  returns NULL the moment this does, which is what makes the URL 404.

Deliberately NOT filtered: ``get_tenant_activity``. The Home feed is where the
``deal.deleted`` event stays visible, which is the readable half of "audited", and
the ledger's existence checks, which must still find a soft-deleted deal.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "rn_008_deal_soft_delete"
down_revision: str | None = "rn_007_mfa_policy_admin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Anchors are substrings of a single line in each definition. Neither contains a
# single quote, so both can be inlined into the DO block below.
_ANCHORS = {
    "public.get_deals_list(uuid)": (
        "FROM deals WHERE tenant_id = p_tenant_id",
        "FROM deals WHERE tenant_id = p_tenant_id AND deleted_at IS NULL",
    ),
    "public.get_workspace_payload(uuid,text)": (
        "WHERE id = p_deal_id AND tenant_id = p_tenant_id",
        "WHERE id = p_deal_id AND tenant_id = p_tenant_id AND deleted_at IS NULL",
    ),
}


def _add_filter(signature: str, find: str, to: str) -> None:
    op.execute(
        f"""
        DO $$
        DECLARE definition TEXT;
        BEGIN
            SELECT pg_get_functiondef('{signature}'::regprocedure) INTO definition;
            IF position('{find}' IN definition) = 0 THEN
                RAISE EXCEPTION 'soft-delete anchor missing from %: %',
                    '{signature}', left('{find}', 70);
            END IF;
            definition := replace(definition, '{find}', '{to}');
            EXECUTE definition;
        END $$;
        """
    )


def _drop_filter(signature: str) -> None:
    op.execute(
        f"""
        DO $$
        DECLARE definition TEXT;
        BEGIN
            SELECT pg_get_functiondef('{signature}'::regprocedure) INTO definition;
            definition := replace(definition, ' AND deleted_at IS NULL', '');
            EXECUTE definition;
        END $$;
        """
    )


def upgrade() -> None:
    # Column first: get_deals_list is LANGUAGE sql and is parsed when created.
    op.execute("ALTER TABLE deals ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_deals_live ON deals (tenant_id) WHERE deleted_at IS NULL")
    for signature, (find, to) in _ANCHORS.items():
        _add_filter(signature, find, to)


def downgrade() -> None:
    # Revert the filters before dropping the column they reference.
    for signature in _ANCHORS:
        _drop_filter(signature)
    op.execute("DROP INDEX IF EXISTS ix_deals_live")
    op.execute("ALTER TABLE deals DROP COLUMN IF EXISTS deleted_at")
