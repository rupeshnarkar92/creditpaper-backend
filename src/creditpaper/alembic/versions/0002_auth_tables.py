"""auth tables

Revision ID: 0002_auth_tables
Revises: 0001_core_tables
Create Date: 2026-09-03 00:00:00

Adds the 4 auth tables that were missing from db.py::_SCHEMA_SQL:
users, domains, invitations, activity_log.

These correspond to the SQLAlchemy models in src/creditpaper/auth/models/.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_auth_tables"
down_revision: str | None = "0001_core_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "domains",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("domain", sa.String(length=255), nullable=False, unique=True),
        sa.Column("admin_email", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_domains_domain", "domains", ["domain"])

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("is_email_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("auth_provider", sa.String(length=20), server_default=sa.text("'email'"), nullable=False),
        sa.Column("is_super_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("role", sa.String(length=20), server_default=sa.text("'user'"), nullable=False),
        sa.Column("domain_id", sa.String(length=36), sa.ForeignKey("domains.id"), nullable=True),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_login_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "invitations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("domain_id", sa.String(length=36), sa.ForeignKey("domains.id"), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_invitations_email", "invitations", ["email"])
    op.create_index("ix_invitations_token", "invitations", ["token"])

    op.create_table(
        "activity_log",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("domain_id", sa.String(length=36), sa.ForeignKey("domains.id"), nullable=False),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("activity_log")
    op.drop_table("invitations")
    op.drop_table("users")
    op.drop_table("domains")
