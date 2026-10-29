"""Per-tenant worker heartbeat, so the UI can tell queued work from a stopped worker.

The all-tenants worker touches a row for each tenant it discovers, every few seconds.
`worker_alive(tenant)` is true when that row is fresh. Tenant-scoped under forced RLS.
Additive only; the downgrade drops the table and functions.
"""

from alembic import op

revision = "US_worker_heartbeats"
down_revision = "nn_149_worker_heartbeats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS public.worker_heartbeats (
      tenant_id uuid PRIMARY KEY,
      last_seen timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    ALTER TABLE public.worker_heartbeats ENABLE ROW LEVEL SECURITY;
    ALTER TABLE public.worker_heartbeats FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS worker_heartbeats_tenant ON public.worker_heartbeats;
    CREATE POLICY worker_heartbeats_tenant ON public.worker_heartbeats
      USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
      WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
    COMMENT ON POLICY worker_heartbeats_tenant ON public.worker_heartbeats IS
      'Restricts reads and writes to the transaction-local app.tenant_id.';

    CREATE OR REPLACE FUNCTION public.touch_worker_heartbeat(p_tenant uuid)
    RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    BEGIN
      IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
        RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
      END IF;
      INSERT INTO public.worker_heartbeats (tenant_id, last_seen)
        VALUES (p_tenant, clock_timestamp())
        ON CONFLICT (tenant_id) DO UPDATE SET last_seen = EXCLUDED.last_seen;
    END $$;
    COMMENT ON FUNCTION public.touch_worker_heartbeat(uuid) IS
      'Tenant -> void. Records that a worker serving this tenant is alive now. Tenant context must be set.';

    CREATE OR REPLACE FUNCTION public.worker_alive(p_tenant uuid, p_max_age interval DEFAULT interval '30 seconds')
    RETURNS boolean LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    BEGIN
      IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
        RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
      END IF;
      RETURN EXISTS (
        SELECT 1 FROM public.worker_heartbeats
        WHERE tenant_id = p_tenant AND last_seen > clock_timestamp() - p_max_age
      );
    END $$;
    COMMENT ON FUNCTION public.worker_alive(uuid, interval) IS
      'Tenant -> boolean. True when a worker touched this tenant within p_max_age.';

    CREATE OR REPLACE FUNCTION public.deal_queued_document_count(p_tenant uuid, p_deal text)
    RETURNS integer LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    BEGIN
      IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
        RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
      END IF;
      RETURN (
        SELECT count(*)::integer FROM public.deal_document_versions v
        JOIN public.deal_documents d ON d.id = v.document_id AND d.tenant_id = v.tenant_id
        WHERE v.tenant_id = p_tenant AND v.deal_id = p_deal
          AND v.processing_status = 'pending' AND d.archived_at IS NULL
      );
    END $$;
    COMMENT ON FUNCTION public.deal_queued_document_count(uuid,text) IS
      'Tenant, deal -> number of non-archived document versions still queued (processing_status pending).';

    CREATE OR REPLACE FUNCTION public.processing_health(p_tenant uuid, p_deal text)
    RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog,public AS $$
    BEGIN
      IF p_tenant IS DISTINCT FROM NULLIF(current_setting('app.tenant_id',true),'')::uuid THEN
        RAISE EXCEPTION 'Tenant context mismatch' USING ERRCODE='42501';
      END IF;
      IF NOT EXISTS (SELECT 1 FROM public.deals WHERE tenant_id = p_tenant AND id = p_deal) THEN
        RETURN NULL;
      END IF;
      RETURN jsonb_build_object(
        'queued', (
          SELECT count(*)::integer FROM public.deal_document_versions v
          JOIN public.deal_documents d ON d.id = v.document_id AND d.tenant_id = v.tenant_id
          WHERE v.tenant_id = p_tenant AND v.deal_id = p_deal
            AND v.processing_status = 'pending' AND d.archived_at IS NULL
        ),
        'worker_alive', public.worker_alive(p_tenant)
      );
    END $$;
    COMMENT ON FUNCTION public.processing_health(uuid, text) IS
      'Tenant, deal -> one JSONB read containing queued document count and worker liveness.';

    REVOKE ALL ON FUNCTION public.touch_worker_heartbeat(uuid) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.worker_alive(uuid, interval) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.deal_queued_document_count(uuid, text) FROM PUBLIC;
    REVOKE ALL ON FUNCTION public.processing_health(uuid, text) FROM PUBLIC;
    DO $$ DECLARE r text; BEGIN
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
        GRANT SELECT, INSERT, UPDATE ON public.worker_heartbeats TO creditpaper_runtime;
        GRANT EXECUTE ON FUNCTION public.touch_worker_heartbeat(uuid),
          public.worker_alive(uuid, interval),
          public.deal_queued_document_count(uuid, text),
          public.processing_health(uuid, text) TO creditpaper_runtime;
      END IF;
      FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
          EXECUTE format('REVOKE ALL ON public.worker_heartbeats FROM %I', r);
          EXECUTE format('REVOKE ALL ON FUNCTION public.touch_worker_heartbeat(uuid) FROM %I', r);
          EXECUTE format('REVOKE ALL ON FUNCTION public.worker_alive(uuid, interval) FROM %I', r);
          EXECUTE format('REVOKE ALL ON FUNCTION public.deal_queued_document_count(uuid, text) FROM %I', r);
          EXECUTE format('REVOKE ALL ON FUNCTION public.processing_health(uuid, text) FROM %I', r);
        END IF;
      END LOOP;
    END $$;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.deal_queued_document_count(uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS public.processing_health(uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS public.worker_alive(uuid, interval)")
    op.execute("DROP FUNCTION IF EXISTS public.touch_worker_heartbeat(uuid)")
    op.execute("DROP TABLE IF EXISTS public.worker_heartbeats")
