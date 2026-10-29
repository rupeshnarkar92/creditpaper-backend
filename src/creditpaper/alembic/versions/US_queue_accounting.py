"""Expose exact durable upload accounting for folder ingestion.

Revision ID: US_queue_accounting
Revises: US_delivery_lane_fairness
"""

from alembic import op

revision = "US_queue_accounting"
down_revision = "US_delivery_lane_fairness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION public.deal_ingestion_accounting(p_tenant uuid,p_deal text)
        RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE result jsonb;
        BEGIN
          IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
          END IF;
          WITH document_states AS (
            SELECT v.processing_status::text AS state
            FROM public.deal_document_versions v
            JOIN public.deal_documents d ON d.id=v.document_id AND d.tenant_id=v.tenant_id
            WHERE v.tenant_id=p_tenant AND v.deal_id=p_deal
              AND v.superseded_at IS NULL AND d.archived_at IS NULL
          ), documents AS (
            SELECT count(*)::integer AS registered,
              count(*) FILTER (WHERE state IN ('ready','failed'))::integer AS terminal,
              count(*) FILTER (WHERE state IN ('pending','processing'))::integer AS active
            FROM document_states
          ), item_states AS (
            SELECT item.value->>'status' AS state,
                   COALESCE(NULLIF(item.value->>'message',''),'Duplicate content') AS reason
            FROM public.deal_upload_batches batch
            CROSS JOIN LATERAL jsonb_each(batch.items) item
            WHERE batch.tenant_id=p_tenant AND batch.deal_id=p_deal
              AND batch.archive_path IS NOT NULL
          ), inputs AS (
            SELECT count(*) FILTER (WHERE state='duplicate')::integer AS duplicate,
              count(*) FILTER (WHERE state='skipped')::integer AS skipped,
              count(*) FILTER (WHERE state='failed')::integer AS registration_failed,
              COALESCE(jsonb_object_agg(reason,n) FILTER (WHERE reason IS NOT NULL),'{}'::jsonb)
                AS reason_counts
            FROM (
              SELECT state,reason,count(*)::integer AS n
              FROM item_states WHERE state IN ('duplicate','skipped','failed')
              GROUP BY state,reason
            ) counted
          ), totals AS (
            SELECT COALESCE(sum(total),0)::integer AS submitted
            FROM public.deal_upload_batches
            WHERE tenant_id=p_tenant AND deal_id=p_deal AND archive_path IS NOT NULL
          )
          SELECT jsonb_build_object(
            'uploaded',GREATEST(totals.submitted,documents.registered+inputs.duplicate+inputs.skipped+inputs.registration_failed),
            'registered',documents.registered,
            'terminal',documents.terminal,
            'active',documents.active,
            -- These are terminal input outcomes which intentionally have no
            -- new document row. Explicit sub-counts preserve the reason.
            'intentionally_skipped',inputs.duplicate+inputs.skipped+inputs.registration_failed,
            'duplicates',inputs.duplicate,
            'unsupported_or_empty',inputs.skipped,
            'registration_failed',inputs.registration_failed,
            'reason_counts',inputs.reason_counts,
            'accounted',documents.terminal+documents.active+inputs.duplicate+inputs.skipped+inputs.registration_failed,
            'discrepancy',GREATEST(totals.submitted,documents.registered+inputs.duplicate+inputs.skipped+inputs.registration_failed)
              -(documents.terminal+documents.active+inputs.duplicate+inputs.skipped+inputs.registration_failed)
          ) INTO result FROM documents CROSS JOIN inputs CROSS JOIN totals;
          RETURN result;
        END $$;

        CREATE OR REPLACE FUNCTION public.deal_extraction_status_snapshot(
          p_tenant_id uuid,p_deal_id text
        ) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
        DECLARE health jsonb; documents jsonb;
        BEGIN
          IF p_tenant_id IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE='42501';
          END IF;
          health := public.processing_health(p_tenant_id,p_deal_id);
          IF health IS NULL THEN RETURN NULL; END IF;
          SELECT COALESCE(jsonb_agg(to_jsonb(item) ORDER BY item.document_id),'[]'::jsonb)
            INTO documents
            FROM public.deal_extraction_status_rows_v2(p_tenant_id,p_deal_id) AS item;
          RETURN health || jsonb_build_object(
            'documents',documents,
            'active_stages',public.processing_stage_health(p_tenant_id,p_deal_id),
            'accounting',public.deal_ingestion_accounting(p_tenant_id,p_deal_id)
          );
        END $$;

        REVOKE ALL ON FUNCTION public.deal_ingestion_accounting(uuid,text) FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.deal_ingestion_accounting(uuid,text) TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.deal_ingestion_accounting(uuid,text)")
