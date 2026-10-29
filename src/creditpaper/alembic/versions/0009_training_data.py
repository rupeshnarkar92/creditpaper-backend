"""Create extraction training data tables.

Revision ID: 0009_training_data
Revises: 0008_extraction_status
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_training_data"
down_revision = "0008_extraction_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Training examples: stores extraction results for improvement
    op.create_table(
        "extraction_training_examples",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("slot_key", sa.String(50), nullable=False),
        sa.Column("extractor_version", sa.String(20), nullable=False, server_default="1.0"),
        sa.Column("instruction_hash", sa.String(64), nullable=False),  # SHA256 of instruction prompt
        sa.Column("model_used", sa.String(100), nullable=False),
        sa.Column("input_pdf_hash", sa.String(64), nullable=False),  # SHA256 of input PDF
        sa.Column("expected_output", sa.dialects.postgresql.JSONB(), nullable=False),  # ground truth
        sa.Column("actual_output", sa.dialects.postgresql.JSONB(), nullable=False),  # what extractor produced
        sa.Column("flags", sa.dialects.postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("quality_score", sa.Float(), nullable=True),  # 0-1, manual or automated
        sa.Column("notes", sa.Text(), nullable=True),  # analyst notes on quality
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "ix_training_examples_tenant_slot", "extraction_training_examples", ["tenant_id", "slot_key"]
    )
    op.create_index("ix_training_examples_deal", "extraction_training_examples", ["deal_id"])

    # Extraction prompt versions: tracks instruction prompt changes
    op.create_table(
        "extraction_prompt_versions",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slot_key", sa.String(50), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("instruction_text", sa.Text(), nullable=False),
        sa.Column("instruction_hash", sa.String(64), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("slot_key", "version", name="uq_prompt_slot_version"),
    )


def downgrade() -> None:
    op.drop_table("extraction_prompt_versions")
    op.drop_table("extraction_training_examples")
