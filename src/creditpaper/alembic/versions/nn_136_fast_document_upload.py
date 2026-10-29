"""Create one atomic, tenant-scoped routine for deal document uploads.

The upload endpoint previously made many sequential database round trips.  This
routine performs duplicate detection, document/version creation, audit logging,
workspace revisioning, and durable parse-job enqueueing in one call.
"""

from alembic import op

revision = "nn_136_fast_document_upload"
down_revision = "US_merge_credit_ops_nn135"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION public.create_deal_document_upload(
          p_tenant_id uuid,
          p_deal_id text,
          p_actor_id uuid,
          p_document_id uuid,
          p_version_id uuid,
          p_display_name text,
          p_original_filename text,
          p_mime_type text,
          p_size_bytes bigint,
          p_content_sha256 text,
          p_storage_path text,
          p_correlation_id text,
          p_uploaded_at timestamptz
        ) RETURNS jsonb
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
          v_document public.deal_documents%ROWTYPE;
          v_duplicate record;
          v_version_number integer;
          v_job_id uuid;
        BEGIN
          IF p_tenant_id IS DISTINCT FROM
             NULLIF(current_setting('app.tenant_id', true), '')::uuid THEN
            RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE = '42501';
          END IF;

          -- Serialize the very short metadata mutation for this deal.  This
          -- prevents two simultaneous uploads of the same name/hash from
          -- creating competing current versions without serializing parsing.
          PERFORM 1
          FROM public.deals d
          WHERE d.id = p_deal_id AND d.tenant_id = p_tenant_id
          FOR UPDATE;
          IF NOT FOUND THEN
            RETURN jsonb_build_object('status', 'not_found');
          END IF;

          SELECT d.display_name, v.version_number
          INTO v_duplicate
          FROM public.deal_document_versions v
          JOIN public.deal_documents d
            ON d.id = v.document_id AND d.tenant_id = v.tenant_id
          WHERE v.tenant_id = p_tenant_id
            AND v.deal_id = p_deal_id
            AND v.content_sha256 = p_content_sha256
            AND v.superseded_at IS NULL
            AND d.archived_at IS NULL
          LIMIT 1;
          IF FOUND THEN
            RETURN jsonb_build_object(
              'status', 'duplicate',
              'display_name', v_duplicate.display_name,
              'version_number', v_duplicate.version_number
            );
          END IF;

          SELECT d.* INTO v_document
          FROM public.deal_documents d
          WHERE d.tenant_id = p_tenant_id
            AND d.deal_id = p_deal_id
            AND d.display_name = p_display_name
            AND d.archived_at IS NULL
          ORDER BY d.created_at
          LIMIT 1;

          IF FOUND THEN
            SELECT COALESCE(MAX(v.version_number), 0) + 1
            INTO v_version_number
            FROM public.deal_document_versions v
            WHERE v.tenant_id = p_tenant_id AND v.document_id = v_document.id;

            UPDATE public.deal_document_versions
            SET superseded_at = p_uploaded_at
            WHERE tenant_id = p_tenant_id
              AND document_id = v_document.id
              AND superseded_at IS NULL;
          ELSE
            INSERT INTO public.deal_documents
              (id, tenant_id, deal_id, display_name, created_by)
            VALUES
              (p_document_id, p_tenant_id, p_deal_id, p_display_name, p_actor_id)
            RETURNING * INTO v_document;
            v_version_number := 1;
          END IF;

          INSERT INTO public.deal_document_versions
            (id, tenant_id, deal_id, document_id, version_number,
             original_filename, mime_type, size_bytes, content_sha256,
             storage_path, uploaded_by, uploaded_at, processing_status)
          VALUES
            (p_version_id, p_tenant_id, p_deal_id, v_document.id,
             v_version_number, p_original_filename, p_mime_type, p_size_bytes,
             p_content_sha256, p_storage_path, p_actor_id, p_uploaded_at, 'pending');

          UPDATE public.deals
          SET workspace_revision = workspace_revision + 1
          WHERE id = p_deal_id AND tenant_id = p_tenant_id;

          INSERT INTO public.deal_audit_events
            (tenant_id, deal_id, actor_id, action, entity_type, entity_id,
             event_metadata, correlation_id, created_at)
          VALUES
            (p_tenant_id, p_deal_id, p_actor_id, 'document.uploaded',
             'deal_document', v_document.id::text,
             jsonb_build_object(
               'document_id', v_document.id,
               'version_number', v_version_number,
               'filename', p_original_filename,
               'mime_type', p_mime_type,
               'size_bytes', p_size_bytes
             ), p_correlation_id, p_uploaded_at);

          INSERT INTO public.processing_jobs
            (tenant_id, deal_id, kind, resource_id, payload)
          VALUES
            (p_tenant_id, p_deal_id, 'parse', p_version_id::text,
             jsonb_build_object(
               'version_id', p_version_id::text,
               'document_id', v_document.id::text
             ))
          ON CONFLICT DO NOTHING
          RETURNING id INTO v_job_id;

          IF v_job_id IS NULL THEN
            SELECT j.id INTO v_job_id
            FROM public.processing_jobs j
            WHERE j.tenant_id = p_tenant_id
              AND j.deal_id = p_deal_id
              AND j.kind = 'parse'
              AND j.resource_id = p_version_id::text
              AND j.status IN ('pending', 'running')
            LIMIT 1;
          END IF;

          RETURN jsonb_build_object(
            'status', 'ok',
            'job_id', v_job_id,
            'document', jsonb_build_object(
              'id', v_document.id,
              'display_name', v_document.display_name,
              'document_kind', v_document.document_kind,
              'created_at', v_document.created_at
            ),
            'version_number', v_version_number
          );
        END
        $$;

        COMMENT ON FUNCTION public.create_deal_document_upload(
          uuid,text,uuid,uuid,uuid,text,text,text,bigint,text,text,text,timestamptz
        ) IS
          'Atomically creates a tenant deal document version, audit event and durable parse job. Returns status, document metadata, version number and job id.';

        REVOKE ALL ON FUNCTION public.create_deal_document_upload(
          uuid,text,uuid,uuid,uuid,text,text,text,bigint,text,text,text,timestamptz
        ) FROM PUBLIC;
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON FUNCTION public.create_deal_document_upload(
              uuid,text,uuid,uuid,uuid,text,text,text,bigint,text,text,text,timestamptz
            ) FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON FUNCTION public.create_deal_document_upload(
              uuid,text,uuid,uuid,uuid,text,text,text,bigint,text,text,text,timestamptz
            ) FROM authenticated;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.create_deal_document_upload(
              uuid,text,uuid,uuid,uuid,text,text,text,bigint,text,text,text,timestamptz
            ) TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """DROP FUNCTION IF EXISTS public.create_deal_document_upload(
        uuid,text,uuid,uuid,uuid,text,text,text,bigint,text,text,text,timestamptz)"""
    )
