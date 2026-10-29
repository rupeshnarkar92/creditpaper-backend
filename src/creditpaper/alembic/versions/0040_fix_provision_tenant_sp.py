"""Fix provision_tenant_for_user: tenants has no slug/domain columns.

0024 was corrected to stop re-creating ``tenants`` with the autogenerate
artefact columns (varchar key, slug/domain), but the 0037 provisioning
procedure was written against that artefact schema: it INSERTed slug/domain
and passed a text id into the uuid key. Every first-time signup — a user
with no membership row — raised (UndefinedColumn, then DatatypeMismatch),
surfaced as StoredProcedureUnavailable and returned 503 from
POST /api/auth/complete-signup. Existing members never hit the INSERT
(idempotent early return), which masked the breakage.

Revision ID: 0040_fix_provision_tenant_sp
Revises: 0039_auth_app_context
Create Date: 2026-09-25
"""

from __future__ import annotations

from alembic import op

revision = "0040_fix_provision_tenant_sp"
down_revision = "0039_auth_app_context"
branch_labels = None
depends_on = None

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

    INSERT INTO public.user_roles (user_id, role_id, tenant_id, assigned_by)
    VALUES (p_user_id, v_role_id, v_tenant_id::uuid, p_user_id)
    ON CONFLICT DO NOTHING;

    RETURN jsonb_build_object('status', 'OK', 'tenant_id', v_tenant_id, 'created', TRUE);
END;
$$;
"""


def upgrade() -> None:
    op.execute(_PROVISION_TENANT)


def downgrade() -> None:
    pass
