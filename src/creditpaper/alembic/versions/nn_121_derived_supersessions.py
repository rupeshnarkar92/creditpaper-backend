"""Move vanished-derived-resolution supersessions behind a stored function."""

from alembic import op

revision = "nn_121_derived_supersessions"
down_revision = "nn_120_ledger_assert_insert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION public.supersede_derived_resolutions(p_tenant_id uuid,p_deal_id text,p_paths text[],p_recorded_at timestamptz) RETURNS integer LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ DECLARE n integer; BEGIN INSERT INTO public.ledger_supersessions(tenant_id,deal_id,resolution_id,recorded_at,reason) SELECT r.tenant_id,r.deal_id,r.id,p_recorded_at,'derivation inputs no longer present' FROM public.ledger_resolutions r WHERE r.tenant_id=p_tenant_id AND r.deal_id=p_deal_id AND r.field_path=ANY(p_paths) AND NOT EXISTS(SELECT 1 FROM public.ledger_supersessions s WHERE s.tenant_id=r.tenant_id AND s.resolution_id=r.id); GET DIAGNOSTICS n=ROW_COUNT; RETURN n; END $$; REVOKE ALL ON FUNCTION public.supersede_derived_resolutions(uuid,text,text[],timestamptz) FROM PUBLIC; GRANT EXECUTE ON FUNCTION public.supersede_derived_resolutions(uuid,text,text[],timestamptz) TO creditpaper_runtime;"""
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.supersede_derived_resolutions(uuid,text,text[],timestamptz)")
