"""templates, template_papers, paper_types tables

Revision ID: 0004_templates_and_paper_types
Revises: 0003_verification_tokens
Create Date: 2026-09-03 00:00:00

Adds:
  - paper_types: fixed list of available paper types
  - templates: one row per template (default + custom) per tenant
  - template_papers: papers uploaded for a specific template
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_templates_and_paper_types"
down_revision: str | None = "0003_verification_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # paper_types: fixed list of available paper types
    op.create_table(
        "paper_types",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    # Seed paper_types
    op.execute(
        """
        INSERT INTO paper_types (id, label, description, is_active, sort_order) VALUES
            ('bridge',       'Bridge Loan',       'Short-term funding until permanent financing', TRUE, 10),
            ('construction', 'Construction Loan',  'For development projects',                       TRUE, 20),
            ('term_loan',    'Term Loan',         'Standard amortising facility',                  TRUE, 30),
            ('acquisition',  'Acquisition',       'Buy-side transaction financing',               TRUE, 40),
            ('refinance',    'Refinance',         'Refinancing existing debt',                     TRUE, 50)
        """
    )

    # templates: one row per template (default + custom)
    op.create_table(
        "templates",
        sa.Column("id", sa.dialects.postgresql.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "paper_types",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "sections", sa.dialects.postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'custom'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # Only ONE default template per tenant
    op.create_index(
        "uq_templates_default_per_tenant",
        "templates",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true"),
    )

    # template_papers: papers uploaded for a specific template
    op.create_table(
        "template_papers",
        sa.Column("id", sa.dialects.postgresql.UUID(), primary_key=True),
        sa.Column("template_id", sa.dialects.postgresql.UUID(), nullable=False),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("is_credit_paper", sa.Boolean(), nullable=False),
        sa.Column("approval_detected", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_index("ix_template_papers_template", "template_papers", ["template_id"])


def downgrade() -> None:
    op.drop_index("ix_template_papers_template", table_name="template_papers")
    op.drop_table("template_papers")
    op.drop_index("uq_templates_default_per_tenant", table_name="templates")
    op.drop_table("templates")
    op.drop_table("paper_types")
