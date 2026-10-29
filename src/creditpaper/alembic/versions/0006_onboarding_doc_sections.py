"""add extracted_sections and extracted_style to onboarding_documents

Revision ID: 0006_onboarding_doc_sections
Revises: 0005_deal_workspace
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_onboarding_doc_sections"
down_revision = "0005_deal_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "onboarding_documents",
        sa.Column(
            "extracted_sections",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "onboarding_documents",
        sa.Column(
            "extracted_style",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_onboarding_documents_tenant",
        "onboarding_documents",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_onboarding_documents_tenant", table_name="onboarding_documents")
    op.drop_column("onboarding_documents", "extracted_style")
    op.drop_column("onboarding_documents", "extracted_sections")
