"""Return ingestion progress and document states in one tenant-scoped call.

Revision ID: US_ingestion_status_snapshot
Revises: US_ingestion_batch_registration
"""

from alembic import op

revision = "US_ingestion_status_snapshot"
down_revision = "US_ingestion_batch_registration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE FUNCTION public.deal_extraction_status_snapshot(
          p_tenant_id uuid,
          p_deal_id text
        ) RETURNS jsonb
        LANGUAGE plpgsql
        STABLE
        SECURITY INVOKER
        SET search_path=pg_catalog,public
        AS $$
        DECLARE
          health jsonb;
          documents jsonb;
        BEGIN
          IF p_tenant_id IS DISTINCT FROM
             NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
            RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE='42501';
          END IF;

          health := public.processing_health(p_tenant_id,p_deal_id);
          IF health IS NULL THEN
            RETURN NULL;
          END IF;

          SELECT COALESCE(jsonb_agg(to_jsonb(item) ORDER BY item.document_id),'[]'::jsonb)
            INTO documents
            FROM public.deal_extraction_status_rows_v2(p_tenant_id,p_deal_id) AS item;

          RETURN health || jsonb_build_object(
            'documents',documents,
            'active_stages',public.processing_stage_health(p_tenant_id,p_deal_id)
          );
        END $$;

        COMMENT ON FUNCTION public.deal_extraction_status_snapshot(uuid,text) IS
          'Tenant/deal -> document terminal states, progress, worker health, and active stages in one call.';

        REVOKE ALL ON FUNCTION public.deal_extraction_status_snapshot(uuid,text) FROM PUBLIC;
        DO $$ DECLARE role_name text; BEGIN
          FOREACH role_name IN ARRAY ARRAY['anon','authenticated'] LOOP
            IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
              EXECUTE format(
                'REVOKE ALL ON FUNCTION public.deal_extraction_status_snapshot(uuid,text) FROM %I',
                role_name
              );
            END IF;
          END LOOP;
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.deal_extraction_status_snapshot(uuid,text)
              TO creditpaper_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.deal_extraction_status_snapshot(uuid,text)")
