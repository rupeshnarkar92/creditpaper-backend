-- Run with psql and migration/admin credentials. This group role has no password
-- and cannot log in; grant it to a separately provisioned restricted login.
-- Never grant it to the migration owner or use owner credentials for the API.
BEGIN;
DO $$ BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='creditpaper_runtime') THEN
    CREATE ROLE creditpaper_runtime NOLOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO creditpaper_runtime;
DO $$ DECLARE t record; browser_role text; BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
    IF t.tablename LIKE 'ledger_%' OR t.tablename IN
       ('deal_recommendations','fact_labels','ingest_parse_artifacts','tenant_llm_usage_events',
        'tenant_integration_audit_events') THEN
      -- Append-only ledger facts, labels, parse artifacts and audit/usage rows.
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM creditpaper_runtime',t.tablename);
      EXECUTE format('GRANT SELECT, INSERT ON TABLE public.%I TO creditpaper_runtime',t.tablename);
    ELSIF t.tablename = 'contradiction_register' THEN
      -- Resolutions update a register entry; deletion is never permitted.
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM creditpaper_runtime',t.tablename);
      EXECUTE format('GRANT SELECT, INSERT, UPDATE ON TABLE public.%I TO creditpaper_runtime',t.tablename);
    ELSIF t.tablename IN ('ingest_proposals','ingest_confirmations') THEN
      -- Intake rows are upserted but never removed by the application.
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM creditpaper_runtime',t.tablename);
      EXECUTE format('GRANT SELECT, INSERT, UPDATE ON TABLE public.%I TO creditpaper_runtime',t.tablename);
    ELSIF t.tablename = 'ingest_held_facts' THEN
      -- Holds are replaced on rerun and deleted when released or discarded.
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM creditpaper_runtime',t.tablename);
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO creditpaper_runtime',t.tablename);
    ELSIF t.tablename IN ('ingest_fact_cache', 'deal_document_versions') THEN
      -- These rows are immutable; processing status changes use the scoped function.
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM creditpaper_runtime',t.tablename);
      EXECUTE format('GRANT SELECT, INSERT, DELETE ON TABLE public.%I TO creditpaper_runtime',t.tablename);
    ELSIF t.tablename = 'processing_jobs' THEN
      -- Leases/status are mutable, but job history is not hard-deleted at runtime.
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM creditpaper_runtime',t.tablename);
      EXECUTE format('GRANT SELECT, INSERT, UPDATE ON TABLE public.%I TO creditpaper_runtime',t.tablename);
    ELSIF t.tablename = 'platform_defaults' THEN
      -- Platform data (no tenant_id): seeded by migrations, read-only at runtime.
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM creditpaper_runtime',t.tablename);
      EXECUTE format('GRANT SELECT ON TABLE public.%I TO creditpaper_runtime',t.tablename);
    ELSIF t.tablename <> 'alembic_version' THEN
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO creditpaper_runtime',t.tablename);
    END IF;
    EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC',t.tablename);
    FOREACH browser_role IN ARRAY ARRAY['anon','authenticated'] LOOP
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=browser_role) THEN
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM %I',t.tablename,browser_role);
      END IF;
    END LOOP;
  END LOOP;
END $$;
GRANT SELECT ON ledger_fact_history TO creditpaper_runtime;
REVOKE ALL ON ledger_fact_history FROM PUBLIC;
DO $$ DECLARE browser_role text; BEGIN
  FOREACH browser_role IN ARRAY ARRAY['anon','authenticated'] LOOP
    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=browser_role) THEN
      EXECUTE format('REVOKE ALL ON ledger_fact_history FROM %I',browser_role);
    END IF;
  END LOOP;
END $$;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO creditpaper_runtime;
-- All-tenants worker discovery (migration 0022): returns tenant ids only.
DO $$ BEGIN
  IF to_regprocedure('public.processing_job_tenants()') IS NOT NULL THEN
    REVOKE EXECUTE ON FUNCTION public.processing_job_tenants() FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION public.processing_job_tenants() TO creditpaper_runtime;
  END IF;
END $$;
-- Auth/signup functions are intentionally revoked from PUBLIC and Supabase's
-- browser roles by their migration. Grant only to the backend runtime role.
DO $$
DECLARE
  signature text;
BEGIN
  FOREACH signature IN ARRAY ARRAY[
    'public.set_document_processing_status(uuid,text,uuid,text,text)',
    'public.set_document_kind_if_unset(uuid,text,uuid,text)',
    'public.get_signup_email_status(text,text,text)',
    'public.get_supabase_auth_context(text)',
    'public.get_authenticated_application_context(text,text,text,text[],text,boolean)',
    'public.get_user_membership(text)',
    'public.resolve_application_user(text,text,text)',
    'public.provision_tenant_for_user(text,text,text)',
    'public.record_password_history(text,text,text,integer)',
    'public.get_password_history_hashes(text,integer)',
    'public.get_password_history_count(text)',
    'public.record_auth_event(text,text,text,text,text,text,jsonb,text)',
    'public.get_home_dashboard(uuid,integer)'
  ] LOOP
    IF to_regprocedure(signature) IS NOT NULL THEN
      EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO creditpaper_runtime', signature);
    END IF;
  END LOOP;
END $$;
COMMIT;
