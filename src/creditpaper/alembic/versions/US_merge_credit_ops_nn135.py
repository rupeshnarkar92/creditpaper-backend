"""Merge the superadmin credit-ops procedures with the nn_049..nn_135 chain.

origin/devel_final carries the stored-procedure and RLS chain
(nn_0049..nn_135, including nn_124_guardrail_rls - the revision the dev
database is actually stamped with). The credit-ops work carries
US_superadmin_credit_ops_sp off US_superadmin_app_context. The two chains
share history up to US_superadmin_app_context and then diverge, so without
this revision the repository has two heads and every alembic command
("heads", "current", "upgrade") refuses to run.

Both branches are additive and were each applied to the dev database
directly, so there is nothing to execute here: the merge point only records
that the two lineages are one line of history again.
"""

revision = "US_merge_credit_ops_nn135"
down_revision = ("US_superadmin_credit_ops_sp", "nn_135_envelope_record_ids")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
