"""merge_heads

Revision ID: 8de6e6b34d56
Revises: 0005_guardrail_rules, 0008_template_paper_extraction, 0011_paper_sections
Create Date: 2026-09-13 16:44:07.195434

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "8de6e6b34d56"
down_revision: str | None = (
    "0005_guardrail_rules",
    "0008_template_paper_extraction",
    "0011_paper_sections",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
