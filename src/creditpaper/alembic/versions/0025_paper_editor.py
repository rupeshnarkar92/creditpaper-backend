"""Persist rich paper section edits and their optimistic revisions."""

from alembic import op

revision = "0025_paper_editor"
down_revision = "0024_extraction_error"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE deal_paper_sections ADD COLUMN editor_value JSONB, "
        "ADD COLUMN edit_revision INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE deal_paper_sections DROP COLUMN editor_value, DROP COLUMN edit_revision")
