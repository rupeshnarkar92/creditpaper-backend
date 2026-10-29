"""Move validated onboarding document reads behind a tenant-scoped function."""

from alembic import op

revision = "nn_056_onboard_reads"
down_revision = "nn_0055_processing_functions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.validated_onboarding_documents(p_tenant_id uuid,p_require_sections boolean)
        RETURNS TABLE(id uuid,filename text,extracted_sections jsonb)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
            SELECT d.id,d.filename,d.extracted_sections FROM public.onboarding_documents d
            WHERE d.tenant_id=p_tenant_id AND d.status='validated' AND d.approval_detected=TRUE
              AND (NOT p_require_sections OR jsonb_array_length(COALESCE(d.extracted_sections,'[]'::jsonb))>0)
            ORDER BY d.created_at
        $$;
        REVOKE ALL ON FUNCTION public.validated_onboarding_documents(uuid,boolean) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.validated_onboarding_documents(uuid,boolean) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.validated_onboarding_documents(uuid,boolean)")
