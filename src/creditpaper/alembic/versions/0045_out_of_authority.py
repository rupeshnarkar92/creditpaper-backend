"""A disagreement from outside a concept's authority is noted, never blocking.

Revision ID: 0045_out_of_authority
Revises: 0044_generation_runs

DECISIONS 2026-10-09: a valuation restating the facility limit differently does
not hold the signed term sheet's figure, because the valuation is not a source
of that concept. The register records it as `out_of_authority`, status `noted`.
"""

from __future__ import annotations

from alembic import op

revision = "0045_out_of_authority"
down_revision = "0044_generation_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE contradiction_register DROP CONSTRAINT IF EXISTS contradiction_register_diff_class_check;
        ALTER TABLE contradiction_register ADD CONSTRAINT contradiction_register_diff_class_check
            CHECK (diff_class IN ('contradiction', 'basis_difference', 'rounding', 'superseded', 'temporal',
                                  'out_of_authority'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM contradiction_register WHERE diff_class = 'out_of_authority';
        ALTER TABLE contradiction_register DROP CONSTRAINT IF EXISTS contradiction_register_diff_class_check;
        ALTER TABLE contradiction_register ADD CONSTRAINT contradiction_register_diff_class_check
            CHECK (diff_class IN ('contradiction', 'basis_difference', 'rounding', 'superseded', 'temporal'));
        """
    )
