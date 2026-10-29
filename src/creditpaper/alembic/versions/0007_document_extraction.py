"""deal document extractions (per-version text + chunks + classification)

Revision ID: 0007_document_extraction
Revises: 0006_onboarding_doc_sections
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_document_extraction"
down_revision = "0006_onboarding_doc_sections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deal_document_extractions",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("page_texts", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("chunks", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("sheet_count", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("classification", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("processing_status", sa.Text(), nullable=False),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["deal_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["deal_document_versions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("version_id", name="uq_deal_document_extractions_version"),
    )
    op.create_index(
        "ix_deal_document_extractions_tenant_deal_doc_ver",
        "deal_document_extractions",
        ["tenant_id", "deal_id", "document_id", sa.text("version_number DESC")],
    )
    op.execute(
        "CREATE INDEX ix_deal_document_extractions_chunks ON deal_document_extractions USING gin (chunks)"
    )
    op.execute(
        "CREATE INDEX ix_deal_document_extractions_classification ON deal_document_extractions USING gin (classification)"
    )

    op.add_column(
        "deal_document_versions",
        sa.Column("processing_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deal_document_versions", "processing_error")
    op.execute("DROP INDEX IF EXISTS ix_deal_document_extractions_classification")
    op.execute("DROP INDEX IF EXISTS ix_deal_document_extractions_chunks")
    op.drop_index("ix_deal_document_extractions_tenant_deal_doc_ver", table_name="deal_document_extractions")
    op.drop_table("deal_document_extractions")
