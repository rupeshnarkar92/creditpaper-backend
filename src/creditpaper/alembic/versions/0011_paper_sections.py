"""Create deal_paper_sections table for paper generation output.

Revision ID: 0011_paper_sections
Revises: 0010_fact_review_queue
Create Date: 2026-09-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_paper_sections"
down_revision = "0010_fact_review_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deal_paper_sections",
        sa.Column(
            "deal_id",
            sa.Text(),
            sa.ForeignKey("deals.id"),
            primary_key=True,
        ),
        sa.Column("section", sa.Integer(), primary_key=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("prose", sa.Text(), nullable=True),
        sa.Column(
            "citations",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "prompt_version_id",
            sa.Text(),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_paper_sections_status",
        "deal_paper_sections",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("deal_paper_sections")
