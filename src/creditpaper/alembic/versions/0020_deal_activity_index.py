"""deal activity read index: keyset paging on deal_audit_events.

Revision ID: 0020_deal_activity_index
Revises: 0019_document_slots

Contract: deal Activity (2026-09-15). GET /api/deals/{id}/activity pages newest first on
(created_at, id). The 0005 index `ix_deal_audit_events_tenant_deal_created` covers
(tenant_id, deal_id, created_at DESC) but not the `id` tie-break, so a keyset page
boundary on equal timestamps needs a sort step. This adds the full key.

ADDITIVE ONLY: one new index, nothing dropped or rewritten. The 0005 index stays (the
workspace and audit reads use it); downgrade drops only the index added here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_deal_activity_index"
down_revision: str | None = "0019_document_slots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_deal_audit_events_activity"


def upgrade() -> None:
    op.create_index(
        INDEX,
        "deal_audit_events",
        ["tenant_id", "deal_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="deal_audit_events")
