"""Create auth_audit_events table — structured audit trail for auth actions.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=True),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=True),
        sa.Column("event_metadata", sa.JSON, server_default="{}"),
        sa.Column("correlation_id", sa.String(36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )

    op.create_index(
        "ix_auth_audit_events_tenant_action",
        "auth_audit_events",
        ["tenant_id", "action", "created_at"],
    )
    op.create_index(
        "ix_auth_audit_events_correlation",
        "auth_audit_events",
        ["correlation_id"],
    )
    op.create_index(
        "ix_auth_audit_events_user",
        "auth_audit_events",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_auth_audit_events_user")
    op.drop_index("ix_auth_audit_events_correlation")
    op.drop_index("ix_auth_audit_events_tenant_action")
    op.drop_table("auth_audit_events")
