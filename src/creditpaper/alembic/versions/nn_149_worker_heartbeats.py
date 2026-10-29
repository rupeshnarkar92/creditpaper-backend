"""Compatibility revision for installations stamped before the US_ rename.

The original heartbeat migration was deployed with this revision id and was
later renamed in source.  Alembic revision ids are persistent database data,
so retain the old id as a no-op bridge.  The following US_ migration is
idempotent and owns the schema definition for fresh installations.
"""

revision = "nn_149_worker_heartbeats"
down_revision = "nn_148_processing_roundtrips"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
