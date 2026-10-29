"""Tenant-isolate voice generation state and restrict its routines."""

from alembic import op

revision = "nn_0051_voice_state_security"
down_revision = "nn_0050_prose_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.voice_generation_state ENABLE ROW LEVEL SECURITY;
        ALTER TABLE public.voice_generation_state FORCE ROW LEVEL SECURITY;
        CREATE POLICY voice_generation_state_tenant ON public.voice_generation_state
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''))
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), ''));

        ALTER FUNCTION public.claim_voice_generation_job(varchar,varchar)
            SECURITY INVOKER SET search_path = pg_catalog, public;
        ALTER FUNCTION public.set_voice_generation_state(varchar,varchar,varchar,text)
            SECURITY INVOKER SET search_path = pg_catalog, public;
        ALTER FUNCTION public.get_voice_generation_state(varchar)
            SECURITY INVOKER SET search_path = pg_catalog, public;
        ALTER FUNCTION public.clear_voice_generation_state(varchar)
            SECURITY INVOKER SET search_path = pg_catalog, public;

        REVOKE ALL ON TABLE public.voice_generation_state FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.claim_voice_generation_job(varchar,varchar) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.set_voice_generation_state(varchar,varchar,varchar,text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.get_voice_generation_state(varchar) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.clear_voice_generation_state(varchar) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.claim_voice_generation_job(varchar,varchar),
            public.set_voice_generation_state(varchar,varchar,varchar,text),
            public.get_voice_generation_state(varchar),
            public.clear_voice_generation_state(varchar) TO creditpaper_runtime;

        COMMENT ON FUNCTION public.claim_voice_generation_job(varchar,varchar) IS
            'Claims a tenant voice-generation job unless an active claim exists. Parameters: tenant and job IDs. Return: claim success.';
        COMMENT ON FUNCTION public.set_voice_generation_state(varchar,varchar,varchar,text) IS
            'Stores tenant voice-generation status. Parameters: tenant, status, job ID, error. Return: void.';
        COMMENT ON FUNCTION public.get_voice_generation_state(varchar) IS
            'Returns voice-generation status JSON for the transaction tenant.';
        COMMENT ON FUNCTION public.clear_voice_generation_state(varchar) IS
            'Deletes voice-generation status for the transaction tenant. Return: void.';
        """
    )


def downgrade() -> None:
    raise RuntimeError("Do not remove voice-state tenant isolation; restore a verified backup")
