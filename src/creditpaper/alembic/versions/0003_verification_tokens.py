"""verification tokens table

Revision ID: 0003_verification_tokens
Revises: 0002_auth_tables
Create Date: 2026-09-03 00:00:00

Adds the verification_tokens table used by the auth/security/tokens.py module
for email verification and password reset flows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_verification_tokens"
down_revision: str | None = "0002_auth_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "verification_tokens",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False, unique=True),
        sa.Column("token_type", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_index("ix_verification_tokens_token", "verification_tokens", ["token"])


def downgrade() -> None:
    op.drop_index("ix_verification_tokens_token", table_name="verification_tokens")
    op.drop_table("verification_tokens")
