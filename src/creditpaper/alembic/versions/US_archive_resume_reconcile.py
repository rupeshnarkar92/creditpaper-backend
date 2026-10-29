"""Reconcile committed archive registrations after a worker restart.

Revision ID: US_archive_resume_reconcile
Revises: US_accounting_count_fix
"""

from alembic import op

revision = "US_archive_resume_reconcile"
down_revision = "US_accounting_count_fix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.create_deal_document_upload_batch(
          p_tenant_id uuid,p_deal_id text,p_actor_id uuid,p_uploads jsonb
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE item jsonb; outcome jsonb; results jsonb := '[]'::jsonb;
                existing_version uuid; existing_job uuid;
        BEGIN
          IF p_tenant_id IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE='42501';
          END IF;
          IF jsonb_typeof(p_uploads) IS DISTINCT FROM 'array'
             OR jsonb_array_length(p_uploads) NOT BETWEEN 1 AND 50 THEN
            RAISE EXCEPTION 'upload batch must contain 1 to 50 items' USING ERRCODE='22023';
          END IF;
          FOR item IN SELECT value FROM jsonb_array_elements(p_uploads) LOOP
            existing_version := NULL; existing_job := NULL;
            outcome := public.create_deal_document_upload(
              p_tenant_id,p_deal_id,p_actor_id,
              (item->>'document_id')::uuid,(item->>'version_id')::uuid,
              item->>'display_name',item->>'original_filename',item->>'mime_type',
              (item->>'size_bytes')::bigint,item->>'content_sha256',item->>'storage_path',
              item->>'correlation_id',(item->>'uploaded_at')::timestamptz
            );
            IF outcome->>'status'='ok' AND item ? 'batch_id' THEN
              UPDATE public.processing_jobs
              SET payload=payload || jsonb_build_object('batch_id',item->>'batch_id')
              WHERE tenant_id=p_tenant_id AND id=(outcome->>'job_id')::uuid;
            ELSIF outcome->>'status'='duplicate' AND item ? 'batch_id'
                  AND outcome->>'display_name'=item->>'display_name' THEN
              -- A crash can occur after the upload transaction commits but
              -- before the archive checkpoint. Only a parse job already
              -- fenced to this exact archive batch proves this is that replay;
              -- a same-content file from another path remains a real duplicate.
              SELECT v.id,j.id INTO existing_version,existing_job
              FROM public.deal_document_versions v
              JOIN public.deal_documents d
                ON d.id=v.document_id AND d.tenant_id=v.tenant_id
              JOIN public.processing_jobs j
                ON j.tenant_id=v.tenant_id AND j.deal_id=v.deal_id
               AND j.kind='parse' AND j.resource_id=v.id::text
              WHERE v.tenant_id=p_tenant_id AND v.deal_id=p_deal_id
                AND v.content_sha256=item->>'content_sha256'
                AND v.superseded_at IS NULL AND d.archived_at IS NULL
                AND d.display_name=item->>'display_name'
                AND j.payload->>'batch_id'=item->>'batch_id'
              LIMIT 1;
              IF existing_version IS NOT NULL THEN
                outcome := jsonb_build_object(
                  'status','resumed','version_id',existing_version,'job_id',existing_job
                );
              END IF;
            END IF;
            results := results || jsonb_build_array(
              jsonb_build_object('client_key',item->>'client_key') || outcome
            );
          END LOOP;
          RETURN results;
        END $$;

        -- Repair only retry artifacts proven by the parse job's batch fence.
        WITH rebuilt AS (
          SELECT batch.id,
            jsonb_object_agg(entry.key,
              CASE WHEN entry.value->>'status'='duplicate' AND EXISTS(
                SELECT 1 FROM public.processing_jobs j
                JOIN public.deal_document_versions v
                  ON v.tenant_id=j.tenant_id AND v.deal_id=j.deal_id
                 AND v.id::text=j.resource_id
                JOIN public.deal_documents d
                  ON d.tenant_id=v.tenant_id AND d.id=v.document_id
                WHERE j.tenant_id=batch.tenant_id AND j.deal_id=batch.deal_id
                  AND j.kind='parse' AND j.payload->>'batch_id'=batch.id::text
                  AND d.display_name=entry.key
              ) THEN jsonb_build_object(
                'status','uploaded',
                'version_id',(
                  SELECT v.id FROM public.processing_jobs j
                  JOIN public.deal_document_versions v
                    ON v.tenant_id=j.tenant_id AND v.deal_id=j.deal_id
                   AND v.id::text=j.resource_id
                  JOIN public.deal_documents d
                    ON d.tenant_id=v.tenant_id AND d.id=v.document_id
                  WHERE j.tenant_id=batch.tenant_id AND j.deal_id=batch.deal_id
                    AND j.kind='parse' AND j.payload->>'batch_id'=batch.id::text
                    AND d.display_name=entry.key LIMIT 1
                )
              ) ELSE entry.value END) AS items
          FROM public.deal_upload_batches batch
          CROSS JOIN LATERAL jsonb_each(batch.items) entry
          GROUP BY batch.id
        )
        UPDATE public.deal_upload_batches batch SET items=rebuilt.items
        FROM rebuilt WHERE rebuilt.id=batch.id;
        """
    )


def downgrade() -> None:
    # The previous batch routine is retained in US_archive_batch_prerequisites.
    pass
