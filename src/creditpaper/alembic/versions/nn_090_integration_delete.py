"""Move integration deletion behind a stored function."""

from alembic import op

revision = "nn_090_integration_delete"
down_revision = "nn_089_integration_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.delete_tenant_integration(p_tenant_id uuid,p_id uuid) RETURNS text LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ DELETE FROM public.tenant_integrations WHERE tenant_id=p_tenant_id AND id=p_id RETURNING name $$; REVOKE ALL ON FUNCTION public.delete_tenant_integration(uuid,uuid) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.delete_tenant_integration(uuid,uuid) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.delete_tenant_integration(uuid,uuid)")
