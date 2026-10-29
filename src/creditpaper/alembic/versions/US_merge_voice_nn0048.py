"""Merge US_merge_rn009_us_voice with nn_0048_drop_get_login_user.

Both branches descend from 0043_ingestion_data: the RBAC/lookup and deal-state
branch reaches US_merge_rn009_us_voice through eeba3e2314cf and rn_002..rn_009,
while the paper-workflow and security branch reaches nn_0048_drop_get_login_user
through 0044..nn_0047. This revision rejoins them into a single head.

Revision IDs are capped at 32 characters: alembic_version.version_num is
VARCHAR(32) (rn_001_lookup_tables). The original names were 45 characters and
could never be written to that column, so no database ever recorded them.
"""

revision = "US_merge_voice_nn0048"
down_revision = (
    "US_merge_rn009_us_voice",
    "nn_0048_drop_get_login_user",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
