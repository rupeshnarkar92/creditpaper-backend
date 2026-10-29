"""Indexes for workspace tab read paths.

The workspace page is intentionally split into smaller endpoints. These indexes
support the document/version and slot reads used by those endpoints without
changing their response contracts.
"""

from __future__ import annotations

from alembic import op

revision = "0036_workspace_read_indexes"
down_revision = "0035_workspace_page_sp_domain_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_deal_document_versions_document_tenant_version "
        "ON deal_document_versions (document_id, tenant_id, version_number DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_slot_files_tenant_slot_created "
        "ON document_slot_files (tenant_id, slot_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_slot_files_tenant_slot_created")
    op.execute("DROP INDEX IF EXISTS ix_deal_document_versions_document_tenant_version")
