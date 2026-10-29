"""Fix UUID/text domain comparison in get_workspace_page."""

from alembic import op

revision = "0035_workspace_page_sp_domain_id"
down_revision = "0034_workspace_page_sp_actor_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE definition TEXT;
        BEGIN
            SELECT pg_get_functiondef('get_workspace_page(uuid,text)'::regprocedure) INTO definition;
            definition := replace(definition, 'WHERE id=p_tenant_id)', 'WHERE id::text=p_tenant_id::text)');
            EXECUTE definition;
        END $$;
        """
    )


def downgrade() -> None:
    pass
