"""Register prepared archive documents in bounded batches.

Revision ID: US_ingestion_batch_registration
Revises: US_ingestion_stage_telemetry
"""

from alembic import op

revision = "US_ingestion_batch_registration"
down_revision = "US_ingestion_stage_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION public.create_deal_document_upload_batch(
          p_tenant_id uuid,
          p_deal_id text,
          p_actor_id uuid,
          p_uploads jsonb
        ) RETURNS jsonb
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path=pg_catalog,public
        AS $$
        DECLARE
          item jsonb;
          outcome jsonb;
          results jsonb := '[]'::jsonb;
        BEGIN
          IF p_tenant_id IS DISTINCT FROM
             NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE='42501';
          END IF;
          IF jsonb_typeof(p_uploads) IS DISTINCT FROM 'array'
             OR jsonb_array_length(p_uploads) NOT BETWEEN 1 AND 50 THEN
            RAISE EXCEPTION 'upload batch must contain 1 to 50 items' USING ERRCODE='22023';
          END IF;

          FOR item IN SELECT value FROM jsonb_array_elements(p_uploads)
          LOOP
            outcome := public.create_deal_document_upload(
              p_tenant_id,
              p_deal_id,
              p_actor_id,
              (item->>'document_id')::uuid,
              (item->>'version_id')::uuid,
              item->>'display_name',
              item->>'original_filename',
              item->>'mime_type',
              (item->>'size_bytes')::bigint,
              item->>'content_sha256',
              item->>'storage_path',
              item->>'correlation_id',
              (item->>'uploaded_at')::timestamptz
            );
            results := results || jsonb_build_array(
              jsonb_build_object('client_key',item->>'client_key') || outcome
            );
          END LOOP;
          RETURN results;
        END
        $$;

        COMMENT ON FUNCTION public.create_deal_document_upload_batch(uuid,text,uuid,jsonb)
          IS 'Tenant deal plus 1-50 prepared encrypted uploads -> per-item atomic document/version/audit/parse-job outcomes.';
        REVOKE ALL ON FUNCTION public.create_deal_document_upload_batch(uuid,text,uuid,jsonb)
          FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
            REVOKE ALL ON FUNCTION public.create_deal_document_upload_batch(uuid,text,uuid,jsonb)
              FROM anon;
          END IF;
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
            REVOKE ALL ON FUNCTION public.create_deal_document_upload_batch(uuid,text,uuid,jsonb)
              FROM authenticated;
          END IF;
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.create_deal_document_upload_batch(uuid,text,uuid,jsonb)
              TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.create_deal_document_upload_batch(uuid,text,uuid,jsonb)"
    )
