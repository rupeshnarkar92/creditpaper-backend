"""Combine tenant Home dashboard reads into one stored-procedure call.

The Home screen needs the deal list and only a small recent-activity page. Unlike
get_tenant_activity, this avoids aggregating every event count on each dashboard visit.
"""

from alembic import op

revision = "0038_home_dashboard_sp"
down_revision = "0037_auth_signup_sp"
branch_labels = None
depends_on = None


_HOME_DASHBOARD_FN = """
CREATE OR REPLACE FUNCTION public.get_home_dashboard(
    p_tenant_id UUID,
    p_activity_limit INTEGER DEFAULT 25
) RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_deals JSONB;
    v_activity JSONB;
    v_limit INTEGER := greatest(1, least(coalesce(p_activity_limit, 25), 200));
BEGIN
    v_deals := public.get_deals_list(p_tenant_id);

    WITH candidates AS MATERIALIZED (
        SELECT e.id, e.actor_id, e.action, e.entity_type, e.entity_id,
               e.event_metadata, e.created_at, d.id AS deal_id, d.name AS deal_name
        FROM public.deal_audit_events e
        JOIN public.deals d ON d.id = e.deal_id AND d.tenant_id = e.tenant_id
        WHERE e.tenant_id = p_tenant_id
        ORDER BY e.created_at DESC, e.id DESC
        LIMIT v_limit + 1
    ), page AS MATERIALIZED (
        SELECT * FROM candidates
        ORDER BY created_at DESC, id DESC
        LIMIT v_limit
    )
    SELECT jsonb_build_object(
        'events', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'id', p.id,
                'actor_id', p.actor_id,
                'action', p.action,
                'entity_type', p.entity_type,
                'entity_id', p.entity_id,
                'metadata', p.event_metadata,
                'created_at', p.created_at,
                'deal', jsonb_build_object('id', p.deal_id, 'name', p.deal_name)
            ) ORDER BY p.created_at DESC, p.id DESC)
            FROM page p
        ), '[]'::jsonb),
        'names', coalesce((
            SELECT jsonb_object_agg(
                u.id::text,
                CASE
                    WHEN trim(
                        trim(coalesce(u.first_name, '')) || ' ' || trim(coalesce(u.last_name, ''))
                    ) <> '' THEN trim(
                        trim(coalesce(u.first_name, '')) || ' ' || trim(coalesce(u.last_name, ''))
                    )
                    ELSE split_part(u.email, '@', 1)
                END
            )
            FROM public.users u
            WHERE u.domain_id = p_tenant_id::text
              AND u.id::text IN (SELECT p.actor_id::text FROM page p WHERE p.actor_id IS NOT NULL)
        ), '{}'::jsonb),
        'has_more', EXISTS (SELECT 1 FROM candidates OFFSET v_limit)
    ) INTO v_activity;

    RETURN jsonb_build_object(
        'deals', coalesce(v_deals, '[]'::jsonb),
        'activity', v_activity
    );
END;
$$;
"""

_PRIVILEGES = """
REVOKE ALL ON FUNCTION public.get_home_dashboard(UUID, INTEGER) FROM PUBLIC;
DO $$
DECLARE v_role TEXT;
BEGIN
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
            EXECUTE format('REVOKE ALL ON FUNCTION public.get_home_dashboard(UUID, INTEGER) FROM %I', v_role);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
        GRANT EXECUTE ON FUNCTION public.get_home_dashboard(UUID, INTEGER) TO creditpaper_runtime;
    END IF;
END;
$$;
"""


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_deal_audit_events_tenant_created "
        "ON public.deal_audit_events (tenant_id, created_at DESC, id DESC)"
    )
    op.execute(_HOME_DASHBOARD_FN)
    op.execute(_PRIVILEGES)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.get_home_dashboard(UUID, INTEGER)")
    op.execute("DROP INDEX IF EXISTS public.ix_deal_audit_events_tenant_created")
