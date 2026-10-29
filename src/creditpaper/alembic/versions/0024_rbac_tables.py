"""Create RBAC tables: roles, permissions, role_permissions, user_roles.

Revision ID: 0024
Revises: 0023_deal_recommendations
Create Date: 2026-09-15

tenants itself is created by 0001_core_tables (uuid primary key, matching
the pre-Alembic runtime shim that production was built on). This migration
originally re-created it with a varchar key and slug/domain columns — an
artefact of autogenerating against the unused Tenant model — which broke
every fresh `alembic upgrade head` with DuplicateTable on tenants. The
tenant_id foreign keys are uuid so they type-match tenants.id exactly as
production has them (roles_tenant_id_fkey, user_roles_tenant_id_fkey).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023_deal_recommendations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # roles table
    op.create_table(
        "roles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(),
            sa.ForeignKey("tenants.id"),
            nullable=True,
        ),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("is_system", sa.Boolean, server_default="false"),
        sa.Column("is_default", sa.Boolean, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "name"),
    )

    # permissions table
    op.create_table(
        "permissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), unique=True, nullable=False),
        sa.Column("resource", sa.String(50), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("description", sa.Text),
        sa.UniqueConstraint("resource", "action"),
    )

    # role_permissions table
    op.create_table(
        "role_permissions",
        sa.Column(
            "role_id",
            sa.String(36),
            sa.ForeignKey("roles.id"),
            primary_key=True,
        ),
        sa.Column(
            "permission_id",
            sa.String(36),
            sa.ForeignKey("permissions.id"),
            primary_key=True,
        ),
    )

    # user_roles table — production carries no FK back to users here, so the
    # migration matches that: user_id/assigned_by stay plain varchar columns.
    op.create_table(
        "user_roles",
        sa.Column(
            "user_id",
            sa.String(36),
            primary_key=True,
        ),
        sa.Column(
            "role_id",
            sa.String(36),
            sa.ForeignKey("roles.id"),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(),
            sa.ForeignKey("tenants.id"),
            primary_key=True,
        ),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "assigned_by",
            sa.String(36),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # tenants is owned by 0001_core_tables — never dropped here.
    op.drop_table("user_roles")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
