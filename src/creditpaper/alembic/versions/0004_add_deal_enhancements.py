"""add deal enhancement columns

Revision ID: 0004_add_deal_enhancements
Revises: 0003_verification_tokens
Create Date: 2026-09-04

Adds columns to support the deals list UI:
- facility_amount: dollar amount of the credit facility
- status: deal workflow state (draft, in_review, approved, needs_attention)
- document_count: number of uploaded documents
- document_required: total documents required
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_add_deal_enhancements"
down_revision: str | None = "0003_verification_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deals",
        sa.Column("facility_amount", sa.Numeric(18, 2), nullable=True),
    )
    op.add_column(
        "deals",
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
    )
    op.add_column(
        "deals",
        sa.Column("document_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "deals",
        sa.Column("document_required", sa.Integer(), server_default="25", nullable=False),
    )
    op.create_index("ix_deals_status", "deals", ["status"])
    op.create_index("ix_deals_tenant_status", "deals", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_deals_tenant_status", table_name="deals")
    op.drop_index("ix_deals_status", table_name="deals")
    op.drop_column("deals", "document_required")
    op.drop_column("deals", "document_count")
    op.drop_column("deals", "status")
    op.drop_column("deals", "facility_amount")
