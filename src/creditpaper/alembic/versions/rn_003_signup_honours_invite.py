"""Signup honours pending invitations; invite-accept flow moves into SPs.

Revision ID: rn_003_signup_honours_invite
Revises: rn_002_member_update_audit

Fix-list row 2 (provisioning ignores invitations) plus its 2b follow-up
(invite.py inline SQL). Three functions are added and one is replaced:

  - pending_invitation_for_email(p_email)  -- read-only lookup used by the
    signup pre-checks so an invited address is not hard-stopped with
    "domain already exists" before the account even exists.
  - validate_invitation(p_token)           -- read-only token validation for
    the AcceptInvitation page (statuses mirror the old route checks:
    INVALID / ALREADY_USED / EXPIRED / OK).
  - accept_invitation(p_token, p_password, p_password_hash)
                                          -- the whole set-password flow:
    token validation, role resolution, local profile + membership, invitation
    consumed, audit row, and the best-effort auth.users bcrypt insert (a
    failure there warns and keeps the local account, exactly like the
    previous Python try/except). HTTP status copy stays in the route.
  - provision_tenant_for_user             -- replaced. Before the DOMAIN_TAKEN
    / create-tenant paths, a pending invitation for this email now joins the
    inviting tenant with the invitation's role and consumes the invite:
      status OK, created FALSE, no new firm, one invitation.accepted audit row.
    The lookup requires the invitation's tenant to still be active; a role
    that no longer resolves falls through to the old behaviour with the
    invitation untouched. Personal domains never reach this procedure (the
    API guards run first), so the join stays company-domain only by
    construction -- banked decision: domain-wise access, no personal-domain
    bypass.

The pre-image of provision_tenant_for_user is carried verbatim for
downgrade. ACL posture is unchanged (default PUBLIC EXECUTE).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "rn_003_signup_honours_invite"
down_revision: str | None = "rn_002_member_update_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PENDING_INVITATION_FOR_EMAIL_FN = """
CREATE OR REPLACE FUNCTION public.pending_invitation_for_email(p_email TEXT)
RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_email TEXT := lower(btrim(coalesce(p_email, '')));
    v_inv   invitations%ROWTYPE;
BEGIN
    IF v_email = '' THEN
        RETURN jsonb_build_object('invited', FALSE);
    END IF;

    SELECT * INTO v_inv
      FROM public.invitations i
     WHERE lower(i.email) = v_email
       AND i.accepted = FALSE
       AND (i.expires_at IS NULL OR i.expires_at >= now())
       AND EXISTS (
           SELECT 1 FROM public.tenants t
            WHERE t.id::text = i.domain_id
              AND t.status = 'active'
       )
     ORDER BY i.created_at DESC
     LIMIT 1;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('invited', FALSE);
    END IF;

    RETURN jsonb_build_object(
        'invited',  TRUE,
        'id',       v_inv.id,
        'email',    v_inv.email,
        'domain_id', v_inv.domain_id,
        'role',     v_inv.role
    );
END;
$$;
"""


_VALIDATE_INVITATION_FN = """
CREATE OR REPLACE FUNCTION public.validate_invitation(p_token TEXT)
RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_inv invitations%ROWTYPE;
BEGIN
    IF p_token IS NULL OR btrim(p_token) = '' THEN
        RETURN jsonb_build_object('status', 'INVALID');
    END IF;

    SELECT * INTO v_inv FROM public.invitations WHERE token = p_token;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('status', 'INVALID');
    END IF;
    IF v_inv.accepted THEN
        RETURN jsonb_build_object('status', 'ALREADY_USED');
    END IF;
    IF v_inv.expires_at IS NOT NULL AND v_inv.expires_at < now() THEN
        RETURN jsonb_build_object('status', 'EXPIRED');
    END IF;

    RETURN jsonb_build_object(
        'status',    'OK',
        'id',        v_inv.id,
        'email',     v_inv.email,
        'domain_id', v_inv.domain_id,
        'role',      v_inv.role
    );
END;
$$;
"""


_ACCEPT_INVITATION_FN = """
CREATE OR REPLACE FUNCTION public.accept_invitation(
    p_token         TEXT,
    p_password      TEXT,
    p_password_hash TEXT
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = pg_catalog, public, extensions, auth, pg_temp
AS $$
DECLARE
    v_val         JSONB;
    v_email       TEXT;
    v_tenant      TEXT;
    v_role_id     TEXT;
    v_user_id     TEXT;
    v_instance_id UUID;
    v_existing    UUID;
BEGIN
    v_val := public.validate_invitation(p_token);
    IF v_val->>'status' <> 'OK' THEN
        RETURN v_val;
    END IF;

    v_email  := lower(btrim(v_val->>'email'));
    v_tenant := v_val->>'domain_id';

    IF EXISTS (SELECT 1 FROM public.users u WHERE lower(u.email) = v_email) THEN
        RETURN jsonb_build_object('status', 'EMAIL_EXISTS');
    END IF;

    -- Tenant-specific role first, then the seeded global role (mirrors the
    -- resolution the route used to do inline).
    SELECT r.id INTO v_role_id
      FROM public.roles r
     WHERE (r.tenant_id::text = v_tenant OR r.tenant_id IS NULL)
       AND r.name = v_val->>'role'
     ORDER BY r.tenant_id NULLS LAST
     LIMIT 1;

    IF v_role_id IS NULL THEN
        RETURN jsonb_build_object('status', 'ROLE_NOT_FOUND', 'role', v_val->>'role');
    END IF;

    v_user_id := gen_random_uuid()::text;

    INSERT INTO public.users (id, email, password_hash, is_email_verified, is_active,
                              auth_provider, role, domain_id)
    VALUES (v_user_id, v_email, p_password_hash, TRUE, TRUE, 'email',
            v_val->>'role', v_tenant);

    INSERT INTO public.user_roles (user_id, role_id, tenant_id, assigned_by)
    VALUES (v_user_id, v_role_id, v_tenant::uuid, v_user_id);

    UPDATE public.invitations SET accepted = TRUE WHERE id = v_val->>'id';

    PERFORM record_auth_event(
        gen_random_uuid()::text,
        v_tenant,
        v_user_id,
        'invitation.accepted',
        'invitation',
        v_val->>'id',
        jsonb_build_object(
            'email',    v_email,
            'role',     v_val->>'role',
            'actor_id', v_user_id,
            'via',      'set_password'
        ),
        gen_random_uuid()::text
    );

    -- Best effort: Supabase login needs an auth.users row; the local account
    -- stands even when this block fails (same contract as the old Python
    -- try/except, which only logged). Prefer any non-null instance id: the
    -- instances table can be empty and a bare LIMIT 1 may land on a null.
    BEGIN
        SELECT COALESCE(
                   (SELECT instance_id FROM auth.users
                     WHERE instance_id IS NOT NULL
                     ORDER BY created_at
                     LIMIT 1),
                   (SELECT id FROM auth.instances ORDER BY created_at LIMIT 1)
               )
          INTO v_instance_id;

        IF v_instance_id IS NOT NULL THEN
            INSERT INTO auth.users
                (instance_id, id, aud, role, email, encrypted_password,
                 email_confirmed_at, created_at, updated_at,
                 raw_app_meta_data, raw_user_meta_data, is_super_admin,
                 confirmation_token, email_change, email_change_token_new,
                 recovery_token)
            VALUES
                (v_instance_id, v_user_id::uuid, 'authenticated', 'authenticated',
                 v_email, crypt(p_password, gen_salt('bf')),
                 now(), now(), now(),
                 '{"provider":"email","providers":["email"]}', '{}', false,
                 '', '', '', '')
            ON CONFLICT (id) DO NOTHING;
        END IF;

        SELECT id INTO v_existing
          FROM auth.users
         WHERE lower(email) = v_email
           AND id::text <> v_user_id
         LIMIT 1;

        IF FOUND THEN
            UPDATE auth.users
               SET encrypted_password = crypt(p_password, gen_salt('bf')),
                   email_confirmed_at = now(),
                   updated_at = now()
             WHERE id = v_existing;

            -- Align application ids with the Supabase identity so the JWT
            -- subject matches public.users.id.
            UPDATE public.users SET id = v_existing::text WHERE id = v_user_id;
            UPDATE public.user_roles SET user_id = v_existing::text
             WHERE user_id = v_user_id;
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'auth.users provisioning failed for %: %', v_email, SQLERRM;
    END;

    RETURN jsonb_build_object('status', 'OK');
END;
$$;
"""


# ---------------------------------------------------------------------------
# provision_tenant_for_user -- replaced below with the invitation branch; the
# pre-image (pg_get_functiondef as of this revision) is kept for downgrade.
# ---------------------------------------------------------------------------

_PROVISION_TENANT_FOR_USER_FN = """
CREATE OR REPLACE FUNCTION public.provision_tenant_for_user(p_user_id text, p_email text, p_company_name text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog', 'public', 'auth', 'pg_temp'
AS $function$
DECLARE
    v_email TEXT := lower(btrim(coalesce(p_email, '')));
    v_domain TEXT := lower(split_part(btrim(coalesce(p_email, '')), '@', 2));
    v_tenant_id TEXT;
    v_role_id TEXT;
    v_auth_context JSONB;
    v_new_tenant_id TEXT;
    v_invite JSONB;
BEGIN
    IF v_domain = '' OR position('@' in v_email) = 0 THEN
        RETURN jsonb_build_object('status', 'INVALID_EMAIL');
    END IF;

    -- Confirmed Supabase identity, active provider account, and exact email match.
    v_auth_context := public.get_supabase_auth_context(p_user_id);
    IF v_auth_context IS NULL
       OR v_auth_context->>'email_confirmed_at' IS NULL
       OR (v_auth_context->>'deleted_at') IS NOT NULL
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

    -- A pending invitation joins the inviting tenant instead of bouncing
    -- DOMAIN_TAKEN or creating a second firm: signup honours the invite and
    -- its role. The tenant must still be active; if the role no longer
    -- resolves, fall through with the invitation untouched.
    SELECT jsonb_build_object(
               'id',        i.id,
               'domain_id', i.domain_id,
               'role',      i.role
           )
      INTO v_invite
      FROM public.invitations i
     WHERE lower(i.email) = v_email
       AND i.accepted = FALSE
       AND (i.expires_at IS NULL OR i.expires_at >= now())
       AND EXISTS (
           SELECT 1 FROM public.tenants t
            WHERE t.id::text = i.domain_id
              AND t.status = 'active'
       )
     ORDER BY i.created_at DESC
     LIMIT 1;

    IF v_invite IS NOT NULL THEN
        SELECT r.id INTO v_role_id
          FROM public.roles r
         WHERE (r.tenant_id::text = v_invite->>'domain_id' OR r.tenant_id IS NULL)
           AND r.name = v_invite->>'role'
         ORDER BY r.tenant_id NULLS LAST
         LIMIT 1;

        IF v_role_id IS NOT NULL THEN
            INSERT INTO public.user_roles (user_id, role_id, tenant_id, assigned_by)
            VALUES (p_user_id, v_role_id, (v_invite->>'domain_id')::uuid, p_user_id)
            ON CONFLICT DO NOTHING;

            UPDATE public.invitations SET accepted = TRUE WHERE id = v_invite->>'id';

            PERFORM record_auth_event(
                gen_random_uuid()::text,
                (v_invite->>'domain_id')::text,
                p_user_id,
                'invitation.accepted',
                'invitation',
                v_invite->>'id',
                jsonb_build_object(
                    'email',    v_email,
                    'role',     v_invite->>'role',
                    'actor_id', p_user_id,
                    'via',      'provisioning'
                ),
                gen_random_uuid()::text
            );

            RETURN jsonb_build_object(
                'status',    'OK',
                'tenant_id', v_invite->>'domain_id',
                'created',   FALSE
            );
        END IF;
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
    INSERT INTO public.tenants (id, name, email_domain, status)
    VALUES (v_new_tenant_id::uuid,
            coalesce(nullif(btrim(p_company_name), ''), v_domain),
            v_domain,
            'active')
    ON CONFLICT DO NOTHING
    RETURNING id::text INTO v_tenant_id;

    IF v_tenant_id IS NULL THEN
        RETURN jsonb_build_object('status', 'DOMAIN_TAKEN');
    END IF;

    -- Invitations FK to domains.id; id maps 1:1 with tenants.id so the
    -- invite flow resolves the tenant from the email domain.
    INSERT INTO public.domains (id, domain, admin_email, company, status)
    VALUES (v_tenant_id,
            v_domain,
            v_email,
            coalesce(nullif(btrim(p_company_name), ''), v_domain),
            'active')
    ON CONFLICT (domain) DO NOTHING;

    INSERT INTO public.user_roles (user_id, role_id, tenant_id, assigned_by)
    VALUES (p_user_id, v_role_id, v_tenant_id::uuid, p_user_id)
    ON CONFLICT DO NOTHING;

    RETURN jsonb_build_object('status', 'OK', 'tenant_id', v_tenant_id, 'created', TRUE);
END;
$function$
"""


_PROVISION_TENANT_FOR_USER_PRE_IMAGE = """
CREATE OR REPLACE FUNCTION public.provision_tenant_for_user(p_user_id text, p_email text, p_company_name text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog', 'public', 'auth', 'pg_temp'
AS $function$
DECLARE
    v_email TEXT := lower(btrim(coalesce(p_email, '')));
    v_domain TEXT := lower(split_part(btrim(coalesce(p_email, '')), '@', 2));
    v_tenant_id TEXT;
    v_role_id TEXT;
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
       OR (v_auth_context->>'deleted_at') IS NOT NULL
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
    INSERT INTO public.tenants (id, name, email_domain, status)
    VALUES (v_new_tenant_id::uuid,
            coalesce(nullif(btrim(p_company_name), ''), v_domain),
            v_domain,
            'active')
    ON CONFLICT DO NOTHING
    RETURNING id::text INTO v_tenant_id;

    IF v_tenant_id IS NULL THEN
        RETURN jsonb_build_object('status', 'DOMAIN_TAKEN');
    END IF;

    -- Invitations FK to domains.id; id maps 1:1 with tenants.id so the
    -- invite flow resolves the tenant from the email domain.
    INSERT INTO public.domains (id, domain, admin_email, company, status)
    VALUES (v_tenant_id,
            v_domain,
            v_email,
            coalesce(nullif(btrim(p_company_name), ''), v_domain),
            'active')
    ON CONFLICT (domain) DO NOTHING;

    INSERT INTO public.user_roles (user_id, role_id, tenant_id, assigned_by)
    VALUES (p_user_id, v_role_id, v_tenant_id::uuid, p_user_id)
    ON CONFLICT DO NOTHING;

    RETURN jsonb_build_object('status', 'OK', 'tenant_id', v_tenant_id, 'created', TRUE);
END;
$function$
"""


def upgrade() -> None:
    op.execute(_PENDING_INVITATION_FOR_EMAIL_FN)
    op.execute(_VALIDATE_INVITATION_FN)
    op.execute(_ACCEPT_INVITATION_FN)
    op.execute(_PROVISION_TENANT_FOR_USER_FN)


def downgrade() -> None:
    op.execute(_PROVISION_TENANT_FOR_USER_PRE_IMAGE)
    op.execute("DROP FUNCTION IF EXISTS public.accept_invitation(TEXT, TEXT, TEXT);")
    op.execute("DROP FUNCTION IF EXISTS public.validate_invitation(TEXT);")
    op.execute("DROP FUNCTION IF EXISTS public.pending_invitation_for_email(TEXT);")
