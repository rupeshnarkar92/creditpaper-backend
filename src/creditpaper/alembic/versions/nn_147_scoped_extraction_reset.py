"""Allow extraction reset through a tenant-checked routine, not broad UPDATE.

Forward-only privilege repair. The runtime still cannot update document content.
"""

from alembic import op

revision = "nn_147_scoped_extraction_reset"
down_revision = "nn_146_atomic_slot_confirmation"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE OR REPLACE FUNCTION public.reset_document_extraction(
      p_tenant_id uuid,p_deal_id text,p_version_id uuid) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
    BEGIN
      IF p_tenant_id IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid
      THEN RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501'; END IF;
      UPDATE public.deal_document_versions SET extraction_status='processing',extraction_error=NULL
        WHERE tenant_id=p_tenant_id AND deal_id=p_deal_id AND id=p_version_id;
      RETURN FOUND;
    END $$;
    COMMENT ON FUNCTION public.reset_document_extraction(uuid,text,uuid) IS
      'Tenant, deal, version UUID -> whether an extraction status was reset to processing. Checks transaction tenant; can only change extraction_status and extraction_error, never document content. Narrow definer entry point because runtime has no direct UPDATE privilege.';
    REVOKE ALL ON FUNCTION public.reset_document_extraction(uuid,text,uuid) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.reset_document_extraction(uuid,text,uuid) TO creditpaper_runtime;
    DO $$ DECLARE r text; BEGIN
      FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
          EXECUTE format('REVOKE ALL ON FUNCTION public.reset_document_extraction(uuid,text,uuid) FROM %I',r);
        END IF;
      END LOOP;
    END $$;
    """)


def downgrade():
    raise RuntimeError(
        "Forward-only privilege repair; restore the prior caller with a reviewed forward migration"
    )
