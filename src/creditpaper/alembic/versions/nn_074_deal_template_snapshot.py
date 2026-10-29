"""Move first-generation deal template snapshot writes into PostgreSQL."""

from alembic import op

revision = "nn_074_deal_template_snapshot"
down_revision = "nn_073_template_update"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.write_template_snapshot_if_absent(
            p_tenant_id uuid, p_deal_id text, p_template_id uuid,
            p_template_source text, p_snapshot jsonb
        ) RETURNS boolean LANGUAGE sql SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
            WITH changed AS (
                UPDATE public.deals
                SET template_id=p_template_id, template_source=p_template_source,
                    template_snapshot=p_snapshot
                WHERE id=p_deal_id AND tenant_id=p_tenant_id
                  AND template_snapshot IS NULL
                RETURNING 1
            ) SELECT EXISTS (SELECT 1 FROM changed)
        $$;
        REVOKE ALL ON FUNCTION public.write_template_snapshot_if_absent(uuid,text,uuid,text,jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.write_template_snapshot_if_absent(uuid,text,uuid,text,jsonb) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.write_template_snapshot_if_absent(uuid,text,uuid,text,jsonb)")
