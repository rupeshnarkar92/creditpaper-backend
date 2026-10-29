"""Resolve trusted Supabase identity and application membership in one call."""

from __future__ import annotations

from alembic import op

revision = "0039_auth_app_context"
down_revision = "0038_home_dashboard_sp"
branch_labels = None
depends_on = None


_AUTHENTICATED_CONTEXT = """
CREATE OR REPLACE FUNCTION public.get_authenticated_application_context(
    p_user_id TEXT,
    p_email TEXT,
    p_provider TEXT,
    p_provider_names TEXT[],
    p_microsoft_tenant_id TEXT,
    p_include_onboarding BOOLEAN
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = pg_catalog, public, auth, pg_temp
AS $$
DECLARE
    v_auth JSONB;
    v_application JSONB;
    v_onboarding_completed BOOLEAN := FALSE;
    v_google_identity JSONB;
    v_microsoft_identity JSONB;
    v_tenant TEXT;
    v_issuer TEXT;
    v_email_domain TEXT;
    v_expected_issuer TEXT;
    v_allowed_tenant TEXT := lower(btrim(coalesce(p_microsoft_tenant_id, '')));
    v_sso_allowed BOOLEAN := TRUE;
BEGIN
    v_auth := public.get_supabase_auth_context(p_user_id);
    IF v_auth IS NULL THEN
        RETURN NULL;
    END IF;

    IF v_auth->>'deleted_at' IS NOT NULL
       OR (v_auth->>'banned_until')::timestamptz > now() THEN
        RETURN jsonb_build_object(
            'auth_context', v_auth,
            'sso_allowed', TRUE,
            'application_context', NULL
        );
    END IF;

    IF lower(coalesce(p_provider, '')) = 'google'
       OR 'google' = ANY(coalesce(p_provider_names, ARRAY[]::TEXT[])) THEN
        v_email_domain := lower(split_part(p_email, '@', 2));
        SELECT value INTO v_google_identity
          FROM jsonb_array_elements(coalesce(v_auth->'identities', '[]'::jsonb)) AS identities(value)
         WHERE lower(value->>'provider') = 'google'
         LIMIT 1;
        v_sso_allowed := v_google_identity IS NOT NULL
            AND v_email_domain <> ''
            AND lower(rtrim(coalesce(v_google_identity->>'hd', ''), '.')) = v_email_domain;
    END IF;

    IF lower(coalesce(p_provider, '')) IN ('azure', 'azuread', 'microsoft')
       OR coalesce(p_provider_names, ARRAY[]::TEXT[]) && ARRAY['azure', 'azuread', 'microsoft']::TEXT[] THEN
        v_expected_issuer := 'https://login.microsoftonline.com/' || v_allowed_tenant || '/v2.0';
        SELECT value INTO v_microsoft_identity
          FROM jsonb_array_elements(coalesce(v_auth->'identities', '[]'::jsonb)) AS identities(value)
         WHERE lower(value->>'provider') IN ('azure', 'azuread', 'microsoft')
         LIMIT 1;
        v_tenant := lower(coalesce(
            v_microsoft_identity->>'tid',
            v_microsoft_identity->>'tenant_id',
            v_microsoft_identity->>'tenantId',
            ''
        ));
        v_issuer := lower(rtrim(coalesce(v_microsoft_identity->>'iss', ''), '/'));
        IF v_tenant = '' AND v_issuer = v_expected_issuer THEN
            v_tenant := v_allowed_tenant;
        END IF;
        v_sso_allowed := v_sso_allowed
            AND v_allowed_tenant <> ''
            AND v_microsoft_identity IS NOT NULL
            AND v_tenant = v_allowed_tenant
            AND (v_issuer = '' OR v_issuer = v_expected_issuer);
    END IF;

    -- Do not provision/update an application profile until provider claims
    -- in the trusted identity record satisfy the same policy as the API.
    IF v_sso_allowed THEN
        v_application := public.resolve_application_user(p_user_id, p_email, p_provider);
        IF p_include_onboarding AND v_application->>'tenant_id' IS NOT NULL THEN
            SELECT coalesce((s.state->'completed_steps') ? 'complete', FALSE)
              INTO v_onboarding_completed
              FROM public.onboarding_state s
             WHERE s.tenant_id::text = v_application->>'tenant_id';
            v_application := v_application || jsonb_build_object(
                'onboarding_completed', coalesce(v_onboarding_completed, FALSE)
            );
        END IF;
    END IF;

    RETURN jsonb_build_object(
        'auth_context', v_auth,
        'sso_allowed', v_sso_allowed,
        'application_context', v_application
    );
END;
$$;
"""

_RESTRICT_EXECUTE = """
DO $$
DECLARE
    v_role TEXT;
BEGIN
    REVOKE ALL ON FUNCTION
        public.get_authenticated_application_context(TEXT, TEXT, TEXT, TEXT[], TEXT, BOOLEAN)
        FROM PUBLIC;
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
            EXECUTE 'REVOKE ALL ON FUNCTION '
                || 'public.get_authenticated_application_context('
                || 'TEXT, TEXT, TEXT, TEXT[], TEXT, BOOLEAN) FROM '
                || quote_ident(v_role);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
        GRANT EXECUTE ON FUNCTION
            public.get_authenticated_application_context(TEXT, TEXT, TEXT, TEXT[], TEXT, BOOLEAN)
            TO creditpaper_runtime;
    END IF;
END;
$$;
"""


def upgrade() -> None:
    op.execute(_AUTHENTICATED_CONTEXT)
    op.execute(_RESTRICT_EXECUTE)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.get_authenticated_application_context("
        "TEXT, TEXT, TEXT, TEXT[], TEXT, BOOLEAN)"
    )
