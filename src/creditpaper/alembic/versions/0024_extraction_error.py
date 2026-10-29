"""Persist extraction failures used by the workspace and slot board.

Revision ID: 0024_extraction_error
Revises: 0023_deal_recommendations
"""

from alembic import op

revision = "0024_extraction_error"
down_revision = "0023_deal_recommendations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Some development databases already have this manually added column.
    op.execute("ALTER TABLE deal_document_versions ADD COLUMN IF NOT EXISTS extraction_error TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE deal_document_versions DROP COLUMN extraction_error")
