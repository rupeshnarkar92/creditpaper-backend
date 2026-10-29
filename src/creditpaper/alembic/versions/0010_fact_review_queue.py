"""Create fact review queue for human review of low-confidence extractions.

Revision ID: 0010_fact_review_queue
Revises: 0009_training_data
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_fact_review_queue"
down_revision = "0009_training_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fact_review_queue",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", sa.Text(), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),  # e.g. "economics.facility_amount"
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("extracted_value", sa.Text(), nullable=True),  # what the extractor produced
        sa.Column("extracted_confidence", sa.Float(), nullable=False),
        sa.Column("reviewed_value", sa.Text(), nullable=True),  # what the human entered/corrected
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="pending"
        ),  # pending / approved / corrected / rejected
        sa.Column("reviewer_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_review_queue_tenant_deal", "fact_review_queue", ["tenant_id", "deal_id"])
    op.create_index("ix_review_queue_status", "fact_review_queue", ["status"])


def downgrade() -> None:
    op.drop_table("fact_review_queue")
