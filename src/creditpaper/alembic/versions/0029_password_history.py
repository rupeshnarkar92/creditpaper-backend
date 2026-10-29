"""Password history — the last 5 passwords can never be reused.

Revision ID: 0029_password_history
Revises: 0028_tenant_email_domain
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_password_history"
down_revision: str | None = "0028_tenant_email_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "password_history",
        sa.Column("id", sa.String(36), primary_key=True),
        # Text, not String(36): Supabase user ids are UUID-shaped today but
        # the identity is opaque — truncation here is a data-loss bug that
        # only surfaces when a future id format arrives (or a test id).
        sa.Column("user_id", sa.Text, nullable=False, index=True),
        # argon2 string (salt, params and digest all embedded). Never a plain
        # digest: an unsalted hash of a password is still a password.
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Trimming and candidate scans are always "latest N for one user".
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_password_history_user_recent "
        "ON password_history (user_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_password_history_user_recent")
    op.drop_table("password_history")
