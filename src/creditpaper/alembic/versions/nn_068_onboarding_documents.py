"""Move onboarding document list/delete behind stored functions."""

from alembic import op

revision = "nn_068_onboarding_documents"
down_revision = "nn_067_template_writes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.onboarding_documents(p_tenant_id uuid)
        RETURNS SETOF public.onboarding_documents LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$ SELECT d.* FROM public.onboarding_documents d WHERE d.tenant_id=p_tenant_id ORDER BY d.created_at $$;
        CREATE OR REPLACE FUNCTION public.delete_onboarding_document(p_tenant_id uuid,p_id uuid) RETURNS boolean LANGUAGE sql SECURITY INVOKER SET search_path=pg_catalog,public AS $$ DELETE FROM public.onboarding_documents WHERE id=p_id AND tenant_id=p_tenant_id RETURNING true $$;
        REVOKE ALL ON FUNCTION public.onboarding_documents(uuid),public.delete_onboarding_document(uuid,uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.onboarding_documents(uuid),public.delete_onboarding_document(uuid,uuid) TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.onboarding_documents(uuid),public.delete_onboarding_document(uuid,uuid)"
    )
