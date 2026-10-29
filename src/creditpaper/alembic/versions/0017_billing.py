"""billing: accounts, invoices, billable events.

Revision ID: 0017_billing
Revises: 0016_user_profile_fields

Flow C. One billing account per tenant (plan, status, period, the card Stripe holds, as
brand and last four only). Invoices mirror Stripe's so the Settings page can list them.
`billable_events` is the source of truth for usage, one row per paper's first approved
render (the unit banked on 2026-06-11); Stripe is only the collector. Mirrors the engine's
`supabase/schema.sql` table so both sides count the same thing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_billing"
down_revision: str | None = "0016_user_profile_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "billing_accounts",
        sa.Column("tenant_id", postgresql.UUID(), primary_key=True),
        sa.Column("tier", sa.Text(), nullable=False, server_default="starter"),
        # Not "interval": that is a Postgres keyword and parses ambiguously in bare SQL.
        sa.Column("billing_interval", sa.Text(), nullable=False, server_default="monthly"),
        sa.Column("status", sa.Text(), nullable=False, server_default="trialing"),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stripe_customer_id", sa.Text(), nullable=True, unique=True),
        sa.Column("stripe_subscription_id", sa.Text(), nullable=True, unique=True),
        sa.Column("card_brand", sa.Text(), nullable=True),
        sa.Column("card_last4", sa.String(4), nullable=True),
        sa.Column("card_exp_month", sa.SmallInteger(), nullable=True),
        sa.Column("card_exp_year", sa.SmallInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("tier IN ('starter', 'standard', 'enterprise')", name="ck_billing_accounts_tier"),
        sa.CheckConstraint("billing_interval IN ('monthly', 'annual')", name="ck_billing_accounts_interval"),
        sa.CheckConstraint(
            "status IN ('trialing', 'active', 'past_due', 'canceled')",
            name="ck_billing_accounts_status",
        ),
    )

    op.create_table(
        "billing_invoices",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("stripe_invoice_id", sa.Text(), nullable=True, unique=True),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="AUD"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("pdf_url", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('paid', 'open', 'failed')", name="ck_billing_invoices_status"),
    )
    op.create_index("ix_billing_invoices_tenant_issued", "billing_invoices", ["tenant_id", "issued_at"])

    op.create_table(
        "billable_events",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("deal_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("event_type IN ('paper_first_approved_render')", name="ck_billable_events_type"),
        sa.UniqueConstraint("deal_id", "event_type", name="uq_billable_once_per_deal"),
    )
    op.create_index("ix_billable_events_tenant_occurred", "billable_events", ["tenant_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_billable_events_tenant_occurred", table_name="billable_events")
    op.drop_table("billable_events")
    op.drop_index("ix_billing_invoices_tenant_issued", table_name="billing_invoices")
    op.drop_table("billing_invoices")
    op.drop_table("billing_accounts")
