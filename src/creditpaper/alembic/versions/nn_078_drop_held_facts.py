"""Move held-facts deletion behind a stored function."""

from alembic import op

revision = "nn_078_drop_held_facts"
down_revision = "nn_077_held_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.drop_held_facts(p_tenant_id uuid,p_deal_id text,p_content_hash text) RETURNS integer LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ DECLARE n integer; BEGIN DELETE FROM public.ingest_held_facts WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id AND content_hash=p_content_hash; GET DIAGNOSTICS n = ROW_COUNT; RETURN n; END $$; REVOKE ALL ON FUNCTION public.drop_held_facts(uuid,text,text) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.drop_held_facts(uuid,text,text) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.drop_held_facts(uuid,text,text)")
