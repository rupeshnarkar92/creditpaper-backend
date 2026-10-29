"""Move integration credential lookup behind a stored function."""

from alembic import op

revision = "nn_089_integration_runtime"
down_revision = "nn_088_integration_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.integration_credentials(p_tenant_id uuid,p_id uuid) RETURNS TABLE(encrypted_credentials text,auth_type text,provider text) LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT encrypted_credentials,auth_type,provider FROM public.tenant_integrations WHERE tenant_id=p_tenant_id AND id=p_id $$; REVOKE ALL ON FUNCTION public.integration_credentials(uuid,uuid) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.integration_credentials(uuid,uuid) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.integration_credentials(uuid,uuid)")
