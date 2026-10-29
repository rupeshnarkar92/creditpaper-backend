"""Bind generated paper sections to the exact ledger revision reviewed."""

from alembic import op

revision = "0015_paper_evidence_revision"
down_revision = "0014_ledger_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE deal_paper_sections ADD COLUMN ledger_revision bigint")
    op.execute("ALTER TABLE deal_paper_sections ADD CONSTRAINT ck_paper_revision CHECK(ledger_revision>=0)")


def downgrade() -> None:
    raise RuntimeError("Paper evidence bindings must be preserved; restore a verified backup instead")
