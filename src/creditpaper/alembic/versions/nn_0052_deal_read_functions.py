"""Move deal workspace and activity reads behind tenant-scoped functions."""

from alembic import op

revision = "nn_0052_deal_read_functions"
down_revision = "nn_0051_voice_state_security"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.deal_exists(p_tenant_id uuid, p_deal_id text)
        RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path = pg_catalog, public AS $$
            SELECT EXISTS(SELECT 1 FROM public.deals d
                WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id)
            AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        $$;

        CREATE OR REPLACE FUNCTION public.deal_version_document_id(
            p_tenant_id uuid, p_deal_id text, p_version_id uuid
        ) RETURNS uuid LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path = pg_catalog, public AS $$
            SELECT v.document_id FROM public.deal_document_versions v
            WHERE v.tenant_id = p_tenant_id AND v.deal_id = p_deal_id AND v.id = p_version_id
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        $$;

        CREATE OR REPLACE FUNCTION public.deal_latest_document_version_id(
            p_tenant_id uuid, p_deal_id text, p_document_id uuid
        ) RETURNS uuid LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path = pg_catalog, public AS $$
            SELECT v.id FROM public.deal_document_versions v
            JOIN public.deal_documents d ON d.id = v.document_id AND d.tenant_id = v.tenant_id
            WHERE d.id = p_document_id AND d.deal_id = p_deal_id AND d.tenant_id = p_tenant_id
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            ORDER BY v.version_number DESC LIMIT 1
        $$;

        CREATE OR REPLACE FUNCTION public.deal_transition_rows(p_tenant_id uuid, p_deal_id text)
        RETURNS TABLE (id uuid, from_state text, to_state text, reason text, actor_id uuid,
            created_at timestamptz)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, public AS $$
            SELECT t.id, t.from_state, t.to_state, t.reason, t.actor_id, t.created_at
            FROM public.deal_state_transitions t
            WHERE t.tenant_id = p_tenant_id AND t.deal_id = p_deal_id
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            ORDER BY t.created_at DESC
        $$;

        CREATE OR REPLACE FUNCTION public.deal_audit_rows(p_tenant_id uuid, p_deal_id text)
        RETURNS TABLE (id uuid, actor_id uuid, action text, entity_type text, entity_id text,
            event_metadata jsonb, created_at timestamptz)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, public AS $$
            SELECT e.id, e.actor_id, e.action, e.entity_type, e.entity_id,
                e.event_metadata, e.created_at
            FROM public.deal_audit_events e
            WHERE e.tenant_id = p_tenant_id AND e.deal_id = p_deal_id
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            ORDER BY e.created_at DESC LIMIT 200
        $$;

        CREATE OR REPLACE FUNCTION public.deal_extraction_status_rows(
            p_tenant_id uuid, p_deal_id text
        ) RETURNS TABLE (document_id uuid, version_id uuid, extraction_status varchar)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, public AS $$
            SELECT v.document_id, v.id, v.extraction_status
            FROM public.deal_document_versions v
            JOIN public.deal_documents d ON d.id = v.document_id AND d.tenant_id = v.tenant_id
            WHERE d.deal_id = p_deal_id AND d.tenant_id = p_tenant_id
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            ORDER BY v.document_id, v.version_number
        $$;

        CREATE OR REPLACE FUNCTION public.deal_activity_identity(p_tenant_id uuid, p_deal_id text)
        RETURNS TABLE (id text, name text, created_at timestamptz)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, public AS $$
            SELECT d.id, d.name, d.created_at FROM public.deals d
            WHERE d.tenant_id = p_tenant_id AND d.id = p_deal_id
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        $$;

        CREATE OR REPLACE FUNCTION public.deal_activity_counts(p_tenant_id uuid, p_deal_id text)
        RETURNS TABLE (action text, event_count bigint)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, public AS $$
            SELECT e.action, count(*) FROM public.deal_audit_events e
            WHERE e.tenant_id = p_tenant_id AND e.deal_id = p_deal_id
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            GROUP BY e.action
        $$;

        CREATE OR REPLACE FUNCTION public.deal_activity_page(
            p_tenant_id uuid, p_deal_id text, p_actions text[], p_before_at timestamptz,
            p_before_id uuid, p_limit integer
        ) RETURNS TABLE (id uuid, actor_id uuid, action text, entity_type text, entity_id text,
            event_metadata jsonb, created_at timestamptz)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, public AS $$
            SELECT e.id, e.actor_id, e.action, e.entity_type, e.entity_id,
                e.event_metadata, e.created_at
            FROM public.deal_audit_events e
            WHERE e.tenant_id = p_tenant_id AND e.deal_id = p_deal_id
              AND (p_actions IS NULL OR e.action = ANY(p_actions))
              AND (p_before_at IS NULL OR (e.created_at, e.id) < (p_before_at, p_before_id))
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            ORDER BY e.created_at DESC, e.id DESC LIMIT greatest(0, least(p_limit, 500))
        $$;

        CREATE OR REPLACE FUNCTION public.tenant_actor_names(p_tenant_id uuid, p_actor_ids text[])
        RETURNS TABLE (id varchar, first_name varchar, last_name varchar, email varchar)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, public AS $$
            SELECT u.id, u.first_name, u.last_name, u.email FROM public.users u
            WHERE u.id = ANY(p_actor_ids) AND u.domain_id = p_tenant_id::text
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        $$;

        CREATE OR REPLACE FUNCTION public.tenant_activity_counts(p_tenant_id uuid)
        RETURNS TABLE (action text, event_count bigint)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, public AS $$
            SELECT e.action, count(*) FROM public.deal_audit_events e
            JOIN public.deals d ON d.tenant_id = e.tenant_id AND d.id = e.deal_id
            WHERE e.tenant_id = p_tenant_id
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            GROUP BY e.action
        $$;

        CREATE OR REPLACE FUNCTION public.tenant_activity_page(
            p_tenant_id uuid, p_actions text[], p_before_at timestamptz,
            p_before_id uuid, p_limit integer
        ) RETURNS TABLE (id uuid, actor_id uuid, action text, entity_type text, entity_id text,
            event_metadata jsonb, created_at timestamptz, deal_id text, deal_name text)
        LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, public AS $$
            SELECT e.id, e.actor_id, e.action, e.entity_type, e.entity_id,
                e.event_metadata, e.created_at, d.id, d.name
            FROM public.deal_audit_events e
            JOIN public.deals d ON d.tenant_id = e.tenant_id AND d.id = e.deal_id
            WHERE e.tenant_id = p_tenant_id
              AND (p_actions IS NULL OR e.action = ANY(p_actions))
              AND (p_before_at IS NULL OR (e.created_at, e.id) < (p_before_at, p_before_id))
              AND p_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            ORDER BY e.created_at DESC, e.id DESC LIMIT greatest(0, least(p_limit, 500))
        $$;

        COMMENT ON FUNCTION public.deal_exists(uuid,text) IS 'Checks whether a deal belongs to the transaction tenant. Returns boolean.';
        COMMENT ON FUNCTION public.deal_version_document_id(uuid,text,uuid) IS 'Returns the document ID for one tenant/deal version.';
        COMMENT ON FUNCTION public.deal_latest_document_version_id(uuid,text,uuid) IS 'Returns newest version ID for one tenant deal document.';
        COMMENT ON FUNCTION public.deal_transition_rows(uuid,text) IS 'Returns newest-first state transitions for one tenant deal.';
        COMMENT ON FUNCTION public.deal_audit_rows(uuid,text) IS 'Returns up to 200 newest audit events for one tenant deal.';
        COMMENT ON FUNCTION public.deal_extraction_status_rows(uuid,text) IS 'Returns extraction status for every version in one tenant deal.';
        COMMENT ON FUNCTION public.deal_activity_identity(uuid,text) IS 'Returns activity header identity for one tenant deal.';
        COMMENT ON FUNCTION public.deal_activity_counts(uuid,text) IS 'Returns activity counts grouped by action for one tenant deal.';
        COMMENT ON FUNCTION public.deal_activity_page(uuid,text,text[],timestamptz,uuid,integer) IS 'Returns a bounded keyset-paginated activity page for one tenant deal.';
        COMMENT ON FUNCTION public.tenant_actor_names(uuid,text[]) IS 'Returns display-name fields for selected members of the transaction tenant.';
        COMMENT ON FUNCTION public.tenant_activity_counts(uuid) IS 'Returns tenant-wide activity counts grouped by action.';
        COMMENT ON FUNCTION public.tenant_activity_page(uuid,text[],timestamptz,uuid,integer) IS 'Returns a bounded tenant-wide keyset activity page with deal identity.';

        GRANT EXECUTE ON FUNCTION public.deal_exists(uuid,text),
            public.deal_version_document_id(uuid,text,uuid),
            public.deal_latest_document_version_id(uuid,text,uuid),
            public.deal_transition_rows(uuid,text), public.deal_audit_rows(uuid,text),
            public.deal_extraction_status_rows(uuid,text), public.deal_activity_identity(uuid,text),
            public.deal_activity_counts(uuid,text),
            public.deal_activity_page(uuid,text,text[],timestamptz,uuid,integer),
            public.tenant_actor_names(uuid,text[]), public.tenant_activity_counts(uuid),
            public.tenant_activity_page(uuid,text[],timestamptz,uuid,integer)
            TO creditpaper_runtime;
        """
    )


def downgrade() -> None:
    raise RuntimeError("Do not restore inline deal reads; restore a verified backup")
