"""add extraction_status to deal_document_versions

Revision ID: 0008_extraction_status
Revises: 0007_document_extraction
Create Date: 2026-09-11
"""

from __future__ import annotations

from alembic import op

revision = "0008_extraction_status"
down_revision = "0007_document_extraction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: column may already exist from manual addition
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS ("
        "  SELECT 1 FROM information_schema.columns "
        "  WHERE table_name = 'deal_document_versions' "
        "  AND column_name = 'extraction_status'"
        ") THEN "
        "  ALTER TABLE deal_document_versions ADD COLUMN extraction_status VARCHAR(20) DEFAULT 'pending' NOT NULL;"
        "END IF; END $$;"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS ("
        "  SELECT 1 FROM information_schema.columns "
        "  WHERE table_name = 'deal_document_versions' "
        "  AND column_name = 'extraction_status'"
        ") THEN "
        "  ALTER TABLE deal_document_versions DROP COLUMN extraction_status;"
        "END IF; END $$;"
    )
