"""Fix UUID/text user joins in get_workspace_page."""

from alembic import op

revision = "0033_workspace_page_sp_user_ids"
down_revision = "0032_workspace_page_sp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE definition TEXT;
        BEGIN
            SELECT pg_get_functiondef('get_workspace_page(uuid,text)'::regprocedure) INTO definition;
            definition := replace(definition, 'u.id IN (', 'u.id::text IN (');
            definition := replace(definition, 'u.id=r.author_id', 'u.id::text=r.author_id::text');
            EXECUTE definition;
        END $$;
        """
    )


def downgrade() -> None:
    pass
