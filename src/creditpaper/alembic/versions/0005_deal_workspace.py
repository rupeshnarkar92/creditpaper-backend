"""add deal workspace, state machine, audit, and document tables

Revision ID: 0005_deal_workspace
Revises: 0004_add_deal_enhancements
Create Date: 2026-09-06

Adds the foundation for the Deal Workspace slice 1:
- `deals.current_state` / `state_changed_at` / `state_changed_by` /
  `workspace_revision` for the audited state machine.
- `deal_workspaces` for non-document workspace metadata.
- `deal_state_transitions` (append-only).
- `deal_audit_events` (append-only).
- `deal_documents` (logical document, many versions).
- `deal_document_versions` (append-only per document; version_number
  unique per document_id; content-addressed via content_sha256).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_deal_workspace"
down_revision: str | None = "0004_add_deal_enhancements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deals",
        sa.Column("current_state", sa.Text(), server_default="DRAFT", nullable=False),
    )
    op.add_column(
        "deals",
        sa.Column(
            "state_changed_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )
    op.add_column(
        "deals",
        sa.Column("state_changed_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "deals",
        sa.Column("workspace_revision", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index("ix_deals_tenant_current_state", "deals", ["tenant_id", "current_state"])

    op.create_table(
        "deal_workspaces",
        sa.Column("deal_id", sa.Text(), primary_key=True),
        sa.Column(
            "summary", sa.dialects.postgresql.JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "key_facts", sa.dialects.postgresql.JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "risk_flags", sa.dialects.postgresql.JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "deal_state_transitions",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", sa.Text(), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_deal_state_transitions_tenant_deal_created",
        "deal_state_transitions",
        ["tenant_id", "deal_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "deal_audit_events",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column(
            "event_metadata",
            sa.dialects.postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_deal_audit_events_tenant_deal_created",
        "deal_audit_events",
        ["tenant_id", "deal_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "deal_documents",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("document_kind", sa.Text(), nullable=True),
        sa.Column("created_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "deal_document_versions",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", sa.Text(), nullable=False),
        sa.Column("document_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("uploaded_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "uploaded_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("processing_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("superseded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["deal_documents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("document_id", "version_number", name="uq_deal_document_versions_doc_ver"),
    )
    op.create_index(
        "ix_deal_document_versions_tenant_deal_doc_ver",
        "deal_document_versions",
        ["tenant_id", "deal_id", "document_id", sa.text("version_number DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_deal_document_versions_tenant_deal_doc_ver", table_name="deal_document_versions")
    op.drop_table("deal_document_versions")
    op.drop_table("deal_documents")
    op.drop_index("ix_deal_audit_events_tenant_deal_created", table_name="deal_audit_events")
    op.drop_table("deal_audit_events")
    op.drop_index("ix_deal_state_transitions_tenant_deal_created", table_name="deal_state_transitions")
    op.drop_table("deal_state_transitions")
    op.drop_table("deal_workspaces")
    op.drop_index("ix_deals_tenant_current_state", table_name="deals")
    op.drop_column("deals", "workspace_revision")
    op.drop_column("deals", "state_changed_by")
    op.drop_column("deals", "state_changed_at")
    op.drop_column("deals", "current_state")
