"""Count archive outcome occurrences rather than grouped reason rows.

Revision ID: US_accounting_count_fix
Revises: US_archive_batch_prereq
"""

from alembic import op

revision = "US_accounting_count_fix"
down_revision = "US_archive_batch_prereq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER FUNCTION public.deal_ingestion_accounting(uuid,text)
          RENAME TO deal_ingestion_accounting_grouped_v1;
        CREATE FUNCTION public.deal_ingestion_accounting(p_tenant uuid,p_deal text)
        RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path=pg_catalog,public AS $$
          WITH base AS (
            SELECT public.deal_ingestion_accounting_grouped_v1(p_tenant,p_deal) AS value
          ), item_states AS (
            SELECT item.value->>'status' AS state,
              COALESCE(NULLIF(item.value->>'message',''),'Duplicate content') AS reason
            FROM public.deal_upload_batches batch
            CROSS JOIN LATERAL jsonb_each(batch.items) item
            WHERE batch.tenant_id=p_tenant AND batch.deal_id=p_deal
              AND batch.archive_path IS NOT NULL
          ), counts AS (
            SELECT count(*) FILTER(WHERE state='duplicate')::integer AS duplicate,
              count(*) FILTER(WHERE state='skipped')::integer AS skipped,
              count(*) FILTER(WHERE state='failed')::integer AS registration_failed
            FROM item_states
          ), reasons AS (
            SELECT COALESCE(jsonb_object_agg(reason,n),'{}'::jsonb) AS value
            FROM (
              SELECT reason,count(*)::integer AS n FROM item_states
              WHERE state IN('duplicate','skipped','failed') GROUP BY reason
            ) grouped
          ), corrected AS (
            SELECT base.value,counts.duplicate,counts.skipped,counts.registration_failed,
              reasons.value AS reason_counts,
              COALESCE((base.value->>'terminal')::integer,0) AS terminal,
              COALESCE((base.value->>'active')::integer,0) AS active,
              COALESCE((base.value->>'uploaded')::integer,0) AS uploaded
            FROM base CROSS JOIN counts CROSS JOIN reasons
          )
          SELECT value || jsonb_build_object(
            'duplicates',duplicate,
            'unsupported_or_empty',skipped,
            'registration_failed',registration_failed,
            'intentionally_skipped',duplicate+skipped+registration_failed,
            'reason_counts',reason_counts,
            'accounted',terminal+active+duplicate+skipped+registration_failed,
            'discrepancy',uploaded-(terminal+active+duplicate+skipped+registration_failed)
          ) FROM corrected
        $$;
        REVOKE ALL ON FUNCTION public.deal_ingestion_accounting(uuid,text) FROM PUBLIC;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.deal_ingestion_accounting(uuid,text) TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION public.deal_ingestion_accounting(uuid,text);
        ALTER FUNCTION public.deal_ingestion_accounting_grouped_v1(uuid,text)
          RENAME TO deal_ingestion_accounting;
        """
    )
