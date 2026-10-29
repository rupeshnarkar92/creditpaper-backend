"""add extracted_sections and tags to template_papers

Revision ID: 0008_template_paper_extraction
Revises: 0007_document_extraction
Create Date: 2026-09-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_template_paper_extraction"
down_revision = "0007_document_extraction"
branch_labels = None
# template_papers is created in 0004_templates_and_paper_types (a sibling branch); without this
# dependency a fresh database runs this migration first and fails.
depends_on = "0004_templates_and_paper_types"


def upgrade() -> None:
    op.add_column(
        "template_papers",
        sa.Column(
            "extracted_sections",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "template_papers",
        sa.Column(
            "tags",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("template_papers", "tags")
    op.drop_column("template_papers", "extracted_sections")
