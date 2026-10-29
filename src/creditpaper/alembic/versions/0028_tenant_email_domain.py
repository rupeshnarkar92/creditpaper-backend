"""Record each tenant's email domain and enforce one active tenant per domain.

The domain is the tenant key (flow A): free-mail can never identify a firm, and
two firms cannot share one email domain. The unique index makes the rule
database-enforced so concurrent signups cannot race past the API guard.

Revision ID: 0028_tenant_email_domain
Revises: 0025 (auth_audit_events branch — the branch this database follows)
Create Date: 2026-09-19

Note on lineage: the versions directory currently carries TWO 0024 revisions
(0024_rbac_tables → 0025, and 0024_extraction_error → 0025_paper_editor →
0026 → 0027). Two heads is a pre-existing condition that needs a merge
migration of its own; this revision anchors to the auth branch that the
databases are stamped on. Revisit when the merge migration lands.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028_tenant_email_domain"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: pre-existing tenants have no recorded domain until the backfill
    # script assigns one; forcing NOT NULL here would fabricate data.
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS email_domain TEXT")
    # Some databases were built by the pre-alembic runtime shim, whose tenants
    # table carries no status column; the partial index below needs it.
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active'")
    # Partial (active only): a suspended or deleted tenant must not forever
    # block its domain — an admin can re-create the firm on the same domain.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tenants_email_domain_active "
        "ON tenants (email_domain) WHERE status = 'active' AND email_domain IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_tenants_email_domain_active")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS email_domain")
