"""MFA core: tenant policy + enrolment marker + context SP enrichment.

Revision ID: rn_006_mfa_core
Revises: rn_005_account_profile_fields

AUTH-11 slice P1 (Supabase TOTP, tenant-level enforcement):

- ``tenants.require_mfa`` — per-firm opt-in switch (flow A: optional,
  default off, admin-decision to enforce). Enforcement stays dormant
  until an admin flips it, so this migration changes no behaviour.
- ``users.mfa_enrolled_at`` — local marker that the user has a verified
  TOTP factor; distinguishes ``MFA_REQUIRED`` (challenge) from
  ``MFA_ENROLL_REQUIRED`` (forced enrolment) at the auth choke point.
- ``set_user_mfa_enrolled(p_user_id, p_enrolled)`` — the only writer of
  the marker (Python never does inline DML); idempotent, keeps the
  original timestamp on re-affirm, NULLs it on unenrol/reset.
- ``get_authenticated_application_context`` gains ``require_mfa`` and
  ``mfa_enrolled_at`` inside ``application_context`` — one extra DB call
  is not allowed on the auth hot path, so the two facts ride the pass
  get_current_user already makes.

All three are plpgsql/DML: no ``check_function_bodies`` wrap needed (that
wrap exists only for LANGUAGE sql functions, which Postgres fully parses
at CREATE time). The 0039 body is carried verbatim as the downgrade
pre-image.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "rn_006_mfa_core"
down_revision: str | None = "rn_005_account_profile_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COLUMNS = """
ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS require_mfa BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS mfa_enrolled_at TIMESTAMPTZ;
"""

_SET_USER_MFA_ENROLLED = """
CREATE OR REPLACE FUNCTION public.set_user_mfa_enrolled(
    p_user_id TEXT,
    p_enrolled BOOLEAN
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public, auth, pg_temp
AS $$
DECLARE
    v_row public.users%ROWTYPE;
BEGIN
    UPDATE public.users
       SET mfa_enrolled_at = CASE
               WHEN p_enrolled THEN coalesce(mfa_enrolled_at, now())
               ELSE NULL
           END
     WHERE id::text = p_user_id
     RETURNING * INTO v_row;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', FALSE, 'error', 'USER_NOT_FOUND');
    END IF;

    RETURN jsonb_build_object(
        'ok', TRUE,
        'mfa_enrolled', v_row.mfa_enrolled_at IS NOT NULL,
        'mfa_enrolled_at', v_row.mfa_enrolled_at
    );
END;
$$;
"""

_SET_USER_MFA_ENROLLED_RESTRICT = """
DO $$
DECLARE
    v_role TEXT;
BEGIN
    REVOKE ALL ON FUNCTION public.set_user_mfa_enrolled(TEXT, BOOLEAN) FROM PUBLIC;
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
            EXECUTE 'REVOKE ALL ON FUNCTION public.set_user_mfa_enrolled(TEXT, BOOLEAN) FROM '
                || quote_ident(v_role);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
        GRANT EXECUTE ON FUNCTION public.set_user_mfa_enrolled(TEXT, BOOLEAN)
            TO creditpaper_runtime;
    END IF;
END;
$$;
"""

# 0039 body + MFA enrichment (application_context gains require_mfa and
# mfa_enrolled_at, resolved inside the pass that already runs per request).
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
$$;
"""

# 0039 pre-image — identical except no MFA enrichment block and no
# v_require_mfa declaration.
_AUTHENTICATED_CONTEXT_PRE_IMAGE = """
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


def upgrade() -> None:
    op.execute(_COLUMNS)
    op.execute(_SET_USER_MFA_ENROLLED)
    op.execute(_SET_USER_MFA_ENROLLED_RESTRICT)
    op.execute(_AUTHENTICATED_CONTEXT)


def downgrade() -> None:
    op.execute(_AUTHENTICATED_CONTEXT_PRE_IMAGE)
    op.execute("DROP FUNCTION IF EXISTS public.set_user_mfa_enrolled(TEXT, BOOLEAN)")
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS mfa_enrolled_at")
    op.execute("ALTER TABLE public.tenants DROP COLUMN IF EXISTS require_mfa")
