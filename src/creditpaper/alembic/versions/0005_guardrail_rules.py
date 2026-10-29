"""guardrail rules + evaluations; backfill duplicate-filename fix.

Revision ID: 0005_guardrail_rules
Revises: 0004_templates_and_paper_types

Also idempotently ensures the 0006_duplicate_filenames change (applied to
the shared DB from another branch): duplicate filenames allowed per tenant.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_guardrail_rules"
down_revision: str | None = "0004_templates_and_paper_types"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent backfill so fresh databases match shared-DB state.
    op.execute(
        "ALTER TABLE onboarding_documents DROP CONSTRAINT IF EXISTS uq_onboarding_documents_tenant_filename"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_onboarding_documents_tenant ON onboarding_documents (tenant_id)"
    )

    op.create_table(
        "guardrail_rules",
        sa.Column("id", sa.dialects.postgresql.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False, server_default="info"),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("scope_type", sa.Text(), nullable=False, server_default="global"),
        sa.Column("scope_value", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('wording', 'structure', 'always_do', 'always_flag', 'outside_appetite')",
            name="ck_guardrail_rules_kind",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warn', 'block')",
            name="ck_guardrail_rules_severity",
        ),
        sa.CheckConstraint(
            "scope_type IN ('global', 'module')",
            name="ck_guardrail_rules_scope_type",
        ),
    )
    op.create_index(
        "ix_guardrail_rules_tenant_active",
        "guardrail_rules",
        ["tenant_id", "is_active"],
    )

    op.create_table(
        "guardrail_evaluations",
        sa.Column("id", sa.dialects.postgresql.UUID(), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(), nullable=False),
        sa.Column("rule_id", sa.dialects.postgresql.UUID(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False, server_default="template"),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("measured", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "result IN ('pass', 'flagged', 'error')",
            name="ck_guardrail_evaluations_result",
        ),
    )
    op.create_index(
        "ix_guardrail_evaluations_rule",
        "guardrail_evaluations",
        ["rule_id"],
    )
    op.create_index(
        "ix_guardrail_evaluations_subject",
        "guardrail_evaluations",
        ["subject_type", "subject_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_guardrail_evaluations_subject", table_name="guardrail_evaluations")
    op.drop_index("ix_guardrail_evaluations_rule", table_name="guardrail_evaluations")
    op.drop_table("guardrail_evaluations")
    op.drop_index("ix_guardrail_rules_tenant_active", table_name="guardrail_rules")
    op.drop_table("guardrail_rules")
