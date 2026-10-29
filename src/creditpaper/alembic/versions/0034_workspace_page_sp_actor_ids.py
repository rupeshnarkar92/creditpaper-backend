"""Fix UUID/text activity actor comparison in get_workspace_page."""

from alembic import op

revision = "0034_workspace_page_sp_actor_ids"
down_revision = "0033_workspace_page_sp_user_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE definition TEXT;
        BEGIN
            SELECT pg_get_functiondef('get_workspace_page(uuid,text)'::regprocedure) INTO definition;
            definition := replace(definition, 'SELECT DISTINCT actor_id FROM deal_audit_events',
                                 'SELECT DISTINCT actor_id::text FROM deal_audit_events');
            EXECUTE definition;
        END $$;
        """
    )


def downgrade() -> None:
    pass
