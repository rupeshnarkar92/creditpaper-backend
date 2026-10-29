"""Move signup and Supabase authentication database work into PostgreSQL functions.

Auth-schema reads use narrowly scoped SECURITY DEFINER functions because the
restricted runtime role cannot read Supabase's auth schema directly. They are
owned by Supabase's trusted postgres role, and all routines are revoked from
browser roles. Apply with migration-owner credentials before deploying callers.
"""

from __future__ import annotations

from alembic import op

revision = "0037_auth_signup_sp"
down_revision = "0037_tenant_members_sp"
branch_labels = None
depends_on = None


_SIGNUP_STATUS = """
CREATE OR REPLACE FUNCTION public.get_signup_email_status(
    p_email TEXT,
    p_domain TEXT,
    p_exclude_user_id TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, auth, pg_temp
AS $$
DECLARE
    v_email TEXT := lower(btrim(coalesce(p_email, '')));
    v_domain TEXT := lower(btrim(coalesce(p_domain, '')));
    v_user_id UUID;
    v_encrypted_password TEXT;
    v_app_metadata JSONB;
    v_providers TEXT[] := ARRAY[]::TEXT[];
    v_has_email BOOLEAN := FALSE;
    v_has_google BOOLEAN := FALSE;
    v_has_oauth BOOLEAN := FALSE;
    v_email_registered BOOLEAN := FALSE;
    v_tenant_registered BOOLEAN := FALSE;
    v_auth_domain_registered BOOLEAN := FALSE;
    v_primary TEXT := 'unknown';
BEGIN
    IF v_email <> '' THEN
        SELECT u.id, u.encrypted_password, u.raw_app_meta_data
          INTO v_user_id, v_encrypted_password, v_app_metadata
          FROM auth.users u
         WHERE lower(u.email) = v_email
         LIMIT 1;
        v_email_registered := FOUND;
    END IF;

    IF v_email_registered THEN
        SELECT coalesce(array_agg(DISTINCT lower(i.provider)), ARRAY[]::TEXT[])
          INTO v_providers
          FROM auth.identities i
         WHERE i.user_id = v_user_id;

        IF cardinality(v_providers) = 0 AND v_app_metadata->>'provider' IS NOT NULL THEN
            v_providers := ARRAY[lower(v_app_metadata->>'provider')];
        END IF;
        v_has_email := v_encrypted_password IS NOT NULL OR 'email' = ANY(v_providers);
        v_has_google := 'google' = ANY(v_providers) OR v_app_metadata->>'provider' = 'google';
        v_has_oauth := cardinality(v_providers) > 0 AND NOT ('email' = ANY(v_providers));
        IF NOT v_has_oauth AND v_app_metadata->>'provider' IS NOT NULL THEN
            v_has_oauth := v_app_metadata->>'provider' <> 'email';
        END IF;
        IF v_has_email AND v_has_google THEN
            v_primary := 'mixed';
        ELSIF v_has_email THEN
            v_primary := 'email';
        ELSIF v_has_google THEN
            v_primary := 'google';
        ELSIF cardinality(v_providers) > 0 THEN
            v_primary := v_providers[1];
        END IF;
    END IF;

    IF v_domain <> '' THEN
        SELECT EXISTS (
            SELECT 1 FROM public.tenants t
             WHERE lower(t.email_domain) = v_domain AND t.status = 'active'
        ) INTO v_tenant_registered;
        SELECT EXISTS (
            SELECT 1 FROM auth.users u
             WHERE split_part(lower(u.email), '@', 2) = v_domain
               AND (p_exclude_user_id IS NULL OR u.id::text <> p_exclude_user_id)
        ) INTO v_auth_domain_registered;
    END IF;

    RETURN jsonb_build_object(
        'email_registered', v_email_registered,
        'tenant_domain_registered', v_tenant_registered,
        'auth_domain_registered', v_auth_domain_registered,
        'domain_registered', v_tenant_registered OR v_auth_domain_registered,
        'providers', to_jsonb(v_providers),
        'is_oauth_only', v_has_oauth AND NOT v_has_email,
        'primary', v_primary
    );
END;
$$;
"""

_AUTH_CONTEXT = """
CREATE OR REPLACE FUNCTION public.get_supabase_auth_context(p_user_id TEXT)
RETURNS JSONB
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, auth, pg_temp
AS $$
    SELECT jsonb_build_object(
        'banned_until', u.banned_until,
        'deleted_at', u.deleted_at,
        'email_confirmed_at', u.email_confirmed_at,
        'email', u.email,
        'identities', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'provider', lower(i.provider),
                'hd', i.identity_data->>'hd',
                'tid', coalesce(
                    i.identity_data->>'tid',
                    i.identity_data->>'tenant_id',
                    i.identity_data->>'tenantId'
                ),
                'iss', i.identity_data->>'iss',
                'email_verified', i.identity_data->'email_verified'
            )) FROM auth.identities i WHERE i.user_id = u.id
        ), '[]'::jsonb)
    )
    FROM auth.users u
    WHERE u.id = p_user_id::uuid
    LIMIT 1;
$$;
"""

_USER_MEMBERSHIP = """
CREATE OR REPLACE FUNCTION public.get_user_membership(p_user_id TEXT)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_tenant_id TEXT;
    v_roles JSONB := '[]'::jsonb;
    v_permissions JSONB := '[]'::jsonb;
    v_user_active BOOLEAN := FALSE;
BEGIN
    SELECT coalesce(u.is_active, FALSE) INTO v_user_active
      FROM public.users u
     WHERE u.id::text = p_user_id;

    IF v_user_active IS NOT TRUE THEN
        RETURN jsonb_build_object('tenant_id', NULL, 'roles', '[]'::jsonb,
                                  'permissions', '[]'::jsonb);
    END IF;

    SELECT ur.tenant_id::text INTO v_tenant_id
      FROM public.user_roles ur
      JOIN public.tenants t ON t.id::text = ur.tenant_id::text AND t.status = 'active'
     WHERE ur.user_id = p_user_id
     ORDER BY ur.assigned_at DESC, ur.tenant_id
     LIMIT 1;

    IF v_tenant_id IS NOT NULL THEN
        SELECT coalesce(jsonb_agg(DISTINCT r.name), '[]'::jsonb)
          INTO v_roles
          FROM public.user_roles ur
          JOIN public.roles r ON r.id::text = ur.role_id::text
          JOIN public.tenants t ON t.id::text = ur.tenant_id::text AND t.status = 'active'
         WHERE ur.user_id = p_user_id AND ur.tenant_id::text = v_tenant_id;

        SELECT coalesce(jsonb_agg(DISTINCT p.name), '[]'::jsonb)
          INTO v_permissions
          FROM public.permissions p
          JOIN public.role_permissions rp ON rp.permission_id::text = p.id::text
          JOIN public.user_roles ur ON ur.role_id::text = rp.role_id::text
          JOIN public.tenants t ON t.id::text = ur.tenant_id::text AND t.status = 'active'
         WHERE ur.user_id = p_user_id AND ur.tenant_id::text = v_tenant_id;
    END IF;

    RETURN jsonb_build_object('tenant_id', v_tenant_id, 'roles', v_roles,
                              'permissions', v_permissions);
END;
$$;
"""

_RESOLVE_APPLICATION_USER = """
CREATE OR REPLACE FUNCTION public.resolve_application_user(
    p_user_id TEXT,
    p_email TEXT,
    p_provider TEXT
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_active BOOLEAN;
    v_membership JSONB;
    v_email_conflict BOOLEAN;
BEGIN
    INSERT INTO public.users
        (id, email, password_hash, is_email_verified, auth_provider, is_active, role)
    VALUES (p_user_id, lower(p_email), NULL, TRUE, p_provider, TRUE, 'tenant_admin')
    ON CONFLICT (id) DO UPDATE SET
        email = EXCLUDED.email,
        auth_provider = EXCLUDED.auth_provider,
        is_email_verified = TRUE,
        updated_at = now()
    WHERE lower(public.users.email) = lower(EXCLUDED.email)
    RETURNING is_active INTO v_active;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('is_active', FALSE, 'tenant_id', NULL,
                                  'roles', '[]'::jsonb, 'permissions', '[]'::jsonb);
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM public.users u
         WHERE lower(u.email) = lower(p_email) AND u.id::text <> p_user_id
    ) INTO v_email_conflict;
    IF v_email_conflict THEN
        RAISE EXCEPTION 'The email is already linked to another application profile';
    END IF;

    IF v_active IS NOT TRUE THEN
        RETURN jsonb_build_object('is_active', FALSE, 'tenant_id', NULL,
                                  'roles', '[]'::jsonb, 'permissions', '[]'::jsonb);
    END IF;

    v_membership := public.get_user_membership(p_user_id);
    RETURN v_membership || jsonb_build_object('is_active', v_active);
END;
$$;
"""

_PROVISION_TENANT = """
CREATE OR REPLACE FUNCTION public.provision_tenant_for_user(
    p_user_id TEXT,
    p_email TEXT,
    p_company_name TEXT
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = pg_catalog, public, auth, pg_temp
AS $$
DECLARE
    v_email TEXT := lower(btrim(coalesce(p_email, '')));
    v_domain TEXT := lower(split_part(btrim(coalesce(p_email, '')), '@', 2));
    v_tenant_id TEXT;
    v_role_id TEXT;
    v_slug TEXT;
    v_auth_context JSONB;
    v_new_tenant_id TEXT;
BEGIN
    IF v_domain = '' OR position('@' in v_email) = 0 THEN
        RETURN jsonb_build_object('status', 'INVALID_EMAIL');
    END IF;

    -- Confirmed Supabase identity, active provider account, and exact email match.
    v_auth_context := public.get_supabase_auth_context(p_user_id);
    IF v_auth_context IS NULL
       OR v_auth_context->>'email_confirmed_at' IS NULL
       OR v_auth_context->>'deleted_at' IS NOT NULL
       OR (v_auth_context->>'banned_until')::timestamptz > now()
       OR lower(v_auth_context->>'email') IS DISTINCT FROM v_email THEN
        RETURN jsonb_build_object('status', 'USER_NOT_AUTHORIZED');
    END IF;

    -- Idempotent for an existing active membership.
    v_auth_context := public.get_user_membership(p_user_id);
    v_tenant_id := v_auth_context->>'tenant_id';
    IF v_tenant_id IS NOT NULL THEN
        RETURN jsonb_build_object('status', 'OK', 'tenant_id', v_tenant_id, 'created', FALSE);
    END IF;

    IF EXISTS (
        SELECT 1 FROM public.tenants t
         WHERE lower(t.email_domain) = v_domain AND t.status = 'active'
    ) THEN
        RETURN jsonb_build_object('status', 'DOMAIN_TAKEN');
    END IF;

    -- Reuse seeded tenant_admin role and permissions rather than creating an
    -- unmapped tenant-local role with no role_permissions rows.
    SELECT r.id::text INTO v_role_id
      FROM public.roles r
     WHERE r.tenant_id IS NULL AND r.name = 'tenant_admin'
     ORDER BY r.created_at
     LIMIT 1;
    IF v_role_id IS NULL THEN
        RAISE EXCEPTION 'The platform tenant_admin role has not been seeded';
    END IF;

    v_new_tenant_id := gen_random_uuid()::text;
    v_slug := left(regexp_replace(v_domain, '[^a-z0-9]+', '-', 'g'), 88)
        || '-' || substr(v_new_tenant_id, 1, 8);
    INSERT INTO public.tenants (id, name, slug, domain, email_domain, status)
    VALUES (v_new_tenant_id,
            coalesce(nullif(btrim(p_company_name), ''), v_domain),
            v_slug,
            v_domain,
            v_domain,
            'active')
    ON CONFLICT DO NOTHING
    RETURNING id::text INTO v_tenant_id;

    IF v_tenant_id IS NULL THEN
        RETURN jsonb_build_object('status', 'DOMAIN_TAKEN');
    END IF;

    INSERT INTO public.user_roles (user_id, role_id, tenant_id, assigned_by)
    VALUES (p_user_id, v_role_id, v_tenant_id, p_user_id)
    ON CONFLICT DO NOTHING;

    RETURN jsonb_build_object('status', 'OK', 'tenant_id', v_tenant_id, 'created', TRUE);
END;
$$;
"""

_RECORD_PASSWORD_HISTORY = """
CREATE OR REPLACE FUNCTION public.record_password_history(
    p_id TEXT,
    p_user_id TEXT,
    p_password_hash TEXT,
    p_keep_count INTEGER
) RETURNS INTEGER
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_count INTEGER;
BEGIN
    INSERT INTO public.password_history (id, user_id, password_hash)
    VALUES (p_id, p_user_id, p_password_hash);
    DELETE FROM public.password_history
     WHERE user_id = p_user_id
       AND id NOT IN (
           SELECT id FROM public.password_history
            WHERE user_id = p_user_id
            ORDER BY created_at DESC, id DESC
            LIMIT greatest(1, least(p_keep_count, 5))
       );
    SELECT count(*) INTO v_count FROM public.password_history WHERE user_id = p_user_id;
    RETURN v_count;
END;
$$;
"""

_PASSWORD_HISTORY_HASHES = """
CREATE OR REPLACE FUNCTION public.get_password_history_hashes(p_user_id TEXT, p_limit INTEGER)
RETURNS JSONB
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT coalesce(jsonb_agg(q.password_hash ORDER BY q.created_at DESC, q.id DESC), '[]'::jsonb)
    FROM (
        SELECT password_hash, created_at, id
          FROM public.password_history
         WHERE user_id = p_user_id
         ORDER BY created_at DESC, id DESC
         LIMIT greatest(0, least(p_limit, 5))
    ) q;
$$;
"""

_PASSWORD_HISTORY_COUNT = """
CREATE OR REPLACE FUNCTION public.get_password_history_count(p_user_id TEXT)
RETURNS INTEGER
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT count(*)::integer FROM public.password_history WHERE user_id = p_user_id;
$$;
"""

_RECORD_AUTH_EVENT = """
CREATE OR REPLACE FUNCTION public.record_auth_event(
    p_id TEXT,
    p_tenant_id TEXT,
    p_user_id TEXT,
    p_action TEXT,
    p_entity_type TEXT,
    p_entity_id TEXT,
    p_event_metadata JSONB,
    p_correlation_id TEXT
) RETURNS JSONB
LANGUAGE sql
VOLATILE
SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp
AS $$
    INSERT INTO public.auth_audit_events
        (id, tenant_id, user_id, action, entity_type, entity_id, event_metadata, correlation_id)
    VALUES (p_id, p_tenant_id, p_user_id, p_action, p_entity_type, p_entity_id,
            coalesce(p_event_metadata, '{}'::jsonb), p_correlation_id)
    RETURNING jsonb_build_object(
        'id', id, 'tenant_id', tenant_id, 'user_id', user_id,
        'action', action, 'entity_type', entity_type, 'entity_id', entity_id,
        'event_metadata', event_metadata, 'correlation_id', correlation_id,
        'created_at', created_at
    );
$$;
"""

_FUNCTIONS = (
    _SIGNUP_STATUS,
    _AUTH_CONTEXT,
    _USER_MEMBERSHIP,
    _RESOLVE_APPLICATION_USER,
    _PROVISION_TENANT,
    _RECORD_PASSWORD_HISTORY,
    _PASSWORD_HISTORY_HASHES,
    _PASSWORD_HISTORY_COUNT,
    _RECORD_AUTH_EVENT,
)

_RESTRICT_EXECUTE = """
DO $$
DECLARE
    v_role TEXT;
    v_signature TEXT;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'public.get_signup_email_status(TEXT, TEXT, TEXT)',
        'public.get_supabase_auth_context(TEXT)',
        'public.get_user_membership(TEXT)',
        'public.resolve_application_user(TEXT, TEXT, TEXT)',
        'public.provision_tenant_for_user(TEXT, TEXT, TEXT)',
        'public.record_password_history(TEXT, TEXT, TEXT, INTEGER)',
        'public.get_password_history_hashes(TEXT, INTEGER)',
        'public.get_password_history_count(TEXT)',
        'public.record_auth_event(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, TEXT)'
    ] LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', v_signature);
        FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
                EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %I', v_signature, v_role);
            END IF;
        END LOOP;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO creditpaper_runtime', v_signature);
        END IF;
    END LOOP;
END;
$$;
"""


def upgrade() -> None:
    # The Supabase auth schema (auth.users / auth.identities) exists only on
    # Supabase-hosted databases. Postgres validates SQL-language function
    # bodies at CREATE time, so a bare local database refuses these
    # definitions even though runtime calls always run against Supabase (or
    # the test schema's auth stubs). Skip body validation for this migration:
    # syntax is still parsed, missing relations resolve at call time.
    op.execute("SET LOCAL check_function_bodies = false")
    for function_sql in _FUNCTIONS:
        op.execute(function_sql)
    op.execute("SET LOCAL check_function_bodies = true")
    op.execute(_RESTRICT_EXECUTE)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.record_auth_event(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, TEXT)"
    )
    op.execute("DROP FUNCTION IF EXISTS public.get_password_history_count(TEXT)")
    op.execute("DROP FUNCTION IF EXISTS public.get_password_history_hashes(TEXT, INTEGER)")
    op.execute("DROP FUNCTION IF EXISTS public.record_password_history(TEXT, TEXT, TEXT, INTEGER)")
    op.execute("DROP FUNCTION IF EXISTS public.provision_tenant_for_user(TEXT, TEXT, TEXT)")
    op.execute("DROP FUNCTION IF EXISTS public.resolve_application_user(TEXT, TEXT, TEXT)")
    op.execute("DROP FUNCTION IF EXISTS public.get_user_membership(TEXT)")
    op.execute("DROP FUNCTION IF EXISTS public.get_supabase_auth_context(TEXT)")
    op.execute("DROP FUNCTION IF EXISTS public.get_signup_email_status(TEXT, TEXT, TEXT)")
