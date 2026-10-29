"""Merge rn_009_deal_state_single_source with US_voice_generation_state.

Both branches descend from eeba3e2314cf: the deal-state branch adds
rn_002..rn_009, while the profile/voice branch adds US_profile_created_at
and US_voice_generation_state. This revision rejoins them into a single head.
"""

revision = "US_merge_rn009_us_voice"
down_revision = ("rn_009_deal_state_single_source", "US_voice_generation_state")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
