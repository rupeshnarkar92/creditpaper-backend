"""Use scoped stored procedures for protected document mutations.

Revision ID: US_runtime_doc_update
Revises: US_drop_deals_status_stage
"""

from alembic import op

revision = "US_runtime_doc_update"
down_revision = "US_drop_deals_status_stage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.set_document_processing_status(
            p_tenant_id uuid,
            p_deal_id text,
            p_version_id uuid,
            p_status text,
            p_reason text DEFAULT NULL
        ) RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_tenant_id IS DISTINCT FROM NULLIF(current_setting('app.tenant_id', true), '')::uuid THEN
                RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE = '42501';
            END IF;
            IF p_status NOT IN ('pending', 'processing', 'ready', 'failed') THEN
                RAISE EXCEPTION 'invalid processing status' USING ERRCODE = '22023';
            END IF;
            UPDATE public.deal_document_versions
               SET processing_status = p_status,
                   processing_error = CASE WHEN p_reason IS NULL THEN NULL ELSE left(p_reason, 500) END
             WHERE tenant_id = p_tenant_id AND deal_id = p_deal_id AND id = p_version_id;
            RETURN FOUND;
        END;
        $$;

        CREATE OR REPLACE FUNCTION public.set_document_kind_if_unset(
            p_tenant_id uuid,
            p_deal_id text,
            p_document_id uuid,
            p_document_kind text
        ) RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_tenant_id IS DISTINCT FROM NULLIF(current_setting('app.tenant_id', true), '')::uuid THEN
                RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE = '42501';
            END IF;
            UPDATE public.deal_documents
               SET document_kind = p_document_kind
             WHERE tenant_id = p_tenant_id AND deal_id = p_deal_id
               AND id = p_document_id AND document_kind IS NULL;
            RETURN FOUND;
        END;
        $$;

        REVOKE ALL ON FUNCTION public.set_document_processing_status(uuid,text,uuid,text,text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.set_document_kind_if_unset(uuid,text,uuid,text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.set_document_processing_status(uuid,text,uuid,text,text)
            TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.set_document_kind_if_unset(uuid,text,uuid,text)
            TO creditpaper_runtime;
        REVOKE UPDATE ON TABLE public.deal_document_versions FROM creditpaper_runtime;
        GRANT SELECT, INSERT, DELETE ON TABLE public.deal_document_versions TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; do not re-enable direct document status updates")
