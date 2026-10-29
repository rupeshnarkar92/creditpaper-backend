"""Add composite indexes for stored procedure hot paths.

Missing indexes force sequential scans inside the PL/pgSQL functions:
  - deal_documents: WHERE tenant_id = ? AND deal_id = ? AND archived_at IS NULL
  - document_slot_files: JOIN via document_slots, filtered by tenant_id
"""

from alembic import op

revision = "0031_perf_indexes"
down_revision = "0030_deals_login_functions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_deal_documents_tenant_deal_archived "
        "ON deal_documents (tenant_id, deal_id, archived_at) "
        "WHERE archived_at IS NULL"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_document_slot_files_tenant ON document_slot_files (tenant_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_slot_files_tenant")
    op.execute("DROP INDEX IF EXISTS ix_deal_documents_tenant_deal_archived")
