"""Move onboarding deal-event writes behind a stored function."""

from alembic import op

revision = "nn_071_event_write"
down_revision = "nn_070_model_write"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.record_onboarding_event(p_deal_id text,p_tenant_id uuid,p_stage text,p_status text,p_message text,p_metadata jsonb)
        RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            INSERT INTO public.deal_events(deal_id,tenant_id,stage,status,message,metadata)
            VALUES(p_deal_id,p_tenant_id,p_stage,p_status,p_message,p_metadata)
            ON CONFLICT(deal_id,stage) DO UPDATE SET status=EXCLUDED.status,message=EXCLUDED.message,metadata=EXCLUDED.metadata,created_at=now()
        $$;
        REVOKE ALL ON FUNCTION public.record_onboarding_event(text,uuid,text,text,text,jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.record_onboarding_event(text,uuid,text,text,text,jsonb) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.record_onboarding_event(text,uuid,text,text,text,jsonb)")
