"""Move integration rate-window consumption behind a stored function."""

from alembic import op

revision = "nn_105_integration_quota"
down_revision = "nn_104_slot_file_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.consume_integration_quota(p_tenant_id uuid,p_integration_id uuid,p_limit integer) RETURNS integer LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ INSERT INTO public.tenant_integration_rate_windows(tenant_id,integration_id,window_start,request_count) VALUES(p_tenant_id,p_integration_id,date_trunc('minute',now()),1) ON CONFLICT(tenant_id,integration_id,window_start) DO UPDATE SET request_count=tenant_integration_rate_windows.request_count+1 WHERE tenant_integration_rate_windows.request_count<p_limit RETURNING request_count $$; REVOKE ALL ON FUNCTION public.consume_integration_quota(uuid,uuid,integer) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.consume_integration_quota(uuid,uuid,integer) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.consume_integration_quota(uuid,uuid,integer)")
