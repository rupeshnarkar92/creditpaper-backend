"""Surface users.is_super_admin through the auth context, for the superadmin portal.

The restored /api/superadmin/* router gates every endpoint on
CurrentUser.is_super_admin, but get_authenticated_application_context never
returned the flag, so it was always False after a Supabase sign-in and the
portal stayed unreachable. One key added to the application_context object;
signature and grants unchanged (CREATE OR REPLACE).
"""

from alembic import op

revision = "US_superadmin_app_context"
down_revision = "US_drop_deals_status_stage"
branch_labels = None
depends_on = None

# Body copied verbatim from the deployed SP with exactly one addition:
# the is_super_admin key on application_context.
_AUTH_APP_CONTEXT = """
CREATE OR REPLACE FUNCTION public.get_authenticated_application_context(p_user_id text, p_email text, p_provider text, p_provider_names text[], p_microsoft_tenant_id text, p_include_onboarding boolean)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog', 'public', 'auth', 'pg_temp'
AS $function$
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
    v_require_mfa BOOLEAN;
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

        -- MFA state (AUTH-11): get_current_user enforces tenants.require_mfa
        -- from this same pass; a second query on the auth hot path is not
        -- allowed.
        IF v_application IS NOT NULL THEN
            SELECT coalesce(t.require_mfa, FALSE)
              INTO v_require_mfa
              FROM public.tenants t
             WHERE t.id::text = v_application->>'tenant_id'
             LIMIT 1;
            v_application := v_application || jsonb_build_object(
                'require_mfa', coalesce(v_require_mfa, FALSE),
                'mfa_enrolled_at', (
                    SELECT u.mfa_enrolled_at
                      FROM public.users u
                     WHERE u.id::text = p_user_id
                )
            );
        END IF;

        -- Superadmin flag rides this same pass (portal gate); a second
        -- query on the auth hot path is not allowed.
        IF v_application IS NOT NULL THEN
            v_application := v_application || jsonb_build_object(
                'is_super_admin', coalesce((
                    SELECT u.is_super_admin
                      FROM public.users u
                     WHERE u.id::text = p_user_id
                ), FALSE)
            );
        END IF;

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
$function$"""


def upgrade() -> None:
    op.execute(_AUTH_APP_CONTEXT)


def downgrade() -> None:
    # Re-run the pre-patch body (is_super_admin removed) — same signature,
    # no grant churn.
    op.execute(
        """
CREATE OR REPLACE FUNCTION public.get_authenticated_application_context(p_user_id text, p_email text, p_provider text, p_provider_names text[], p_microsoft_tenant_id text, p_include_onboarding boolean)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog', 'public', 'auth', 'pg_temp'
AS $function$
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
    v_require_mfa BOOLEAN;
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

        -- MFA state (AUTH-11): get_current_user enforces tenants.require_mfa
        -- from this same pass; a second query on the auth hot path is not
        -- allowed.
        IF v_application IS NOT NULL THEN
            SELECT coalesce(t.require_mfa, FALSE)
              INTO v_require_mfa
              FROM public.tenants t
             WHERE t.id::text = v_application->>'tenant_id'
             LIMIT 1;
            v_application := v_application || jsonb_build_object(
                'require_mfa', coalesce(v_require_mfa, FALSE),
                'mfa_enrolled_at', (
                    SELECT u.mfa_enrolled_at
                      FROM public.users u
                     WHERE u.id::text = p_user_id
                )
            );
        END IF;

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
$function$"""
    )
