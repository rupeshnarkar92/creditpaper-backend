"""Move onboarding model snapshot writes behind a stored function."""

from alembic import op

revision = "nn_070_model_write"
down_revision = "nn_069_doc_writes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.save_onboarding_model(p_deal_id text,p_tenant_id uuid,p_model jsonb)
        RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            INSERT INTO public.deal_model_snapshots(deal_id,tenant_id,model) VALUES(p_deal_id,p_tenant_id,p_model)
            ON CONFLICT(deal_id) DO UPDATE SET model=EXCLUDED.model,created_at=now()
            WHERE public.deal_model_snapshots.tenant_id=EXCLUDED.tenant_id
        $$;
        REVOKE ALL ON FUNCTION public.save_onboarding_model(text,uuid,jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.save_onboarding_model(text,uuid,jsonb) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.save_onboarding_model(text,uuid,jsonb)")
