"""user profile fields: first name, last name, job title, phone.

Revision ID: 0016_user_profile_fields
Revises: 0015_paper_evidence_revision

Settings, Account: a person maintains their own name, job title and phone. The name and
job title are what approvals and the audit trail show (D8 needs a named human). Email is
the sign-in identity and is not editable, so it is not touched here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_user_profile_fields"
down_revision: str | None = "0015_paper_evidence_revision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("first_name", sa.String(80), nullable=True))
    op.add_column("users", sa.Column("last_name", sa.String(80), nullable=True))
    op.add_column("users", sa.Column("job_title", sa.String(120), nullable=True))
    op.add_column("users", sa.Column("phone", sa.String(40), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "phone")
    op.drop_column("users", "job_title")
    op.drop_column("users", "last_name")
    op.drop_column("users", "first_name")
