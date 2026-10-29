"""Audit team changes: role/activation writes recorded to auth_audit_events.

Revision ID: rn_002_member_update_audit
Revises: eeba3e2314cf

Closes the "audit role changes" gap from the fix-list row on team & invites:
``update_tenant_member`` (migration 0037) updated ``user_roles`` / ``users``
silently. The function now:

  - takes ``p_actor_id UUID DEFAULT NULL`` (who made the change; back-compat
    for any 4-arg caller via DEFAULT),
  - validates first, then mutates, then writes one ``record_auth_event`` row
    per actual change:
      role change        -> action ``role.assigned``
                            metadata {previous_role, new_role, actor_id}
      is_active toggle   -> action ``user.activated`` / ``user.deactivated``
                            metadata {previous_is_active, new_is_active, actor_id}
  - absorbs the two guards that lived inline in
    ``api/routes/tenant_members.py`` (CLAUDE.md: no business logic in Python
    routes), with identical error strings:
      "You cannot deactivate yourself"
      "Cannot deactivate the last active admin"
    The last-admin guard now only fires when the target is themselves an
    active tenant_admin (the route's version also blocked deactivating a
    non-admin whenever the tenant had a single admin — fixed while moving).
  - casts the uuid parameters with ``::text`` where they meet id columns:
    ``public.users.id`` / ``public.user_roles.user_id`` are ``varchar`` (the
    uuid ids live in ``auth.users``), and ``varchar = uuid`` has no operator —
    the 0037 body carried this latent defect, so PATCH member updates raised
    UndefinedFunction whenever they were actually called. Casting the
    parameter (not the column) keeps the varchar indexes usable.

The old 4-arg signature is dropped so no unaudited overload survives; the
route is updated in the same change. ACL posture is unchanged (default PUBLIC
EXECUTE, exactly as the 0037 function had).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "rn_002_member_update_audit"
down_revision: str | None = "eeba3e2314cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPDATE_TENANT_MEMBER_FN = """
DROP FUNCTION IF EXISTS public.update_tenant_member(UUID, UUID, TEXT, BOOLEAN);

CREATE OR REPLACE FUNCTION public.update_tenant_member(
    p_user_id   UUID,
    p_tenant_id UUID,
    p_role      TEXT DEFAULT NULL,
    p_is_active BOOLEAN DEFAULT NULL,
    p_actor_id  UUID DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_current_role   TEXT;
    v_current_active BOOLEAN;
    v_role_id        UUID;
    v_active_admins  BIGINT;
BEGIN
    -- Verify user exists in this tenant
    SELECT r.name, u.is_active
    INTO v_current_role, v_current_active
    FROM user_roles ur
    JOIN roles r ON r.id = ur.role_id
    JOIN users u ON u.id = ur.user_id
    WHERE ur.user_id = p_user_id::text AND ur.tenant_id = p_tenant_id;

    IF v_current_role IS NULL THEN
        RETURN jsonb_build_object('error', 'User not found in this tenant');
    END IF;

    -- Guard (moved from the route): never deactivate yourself.
    IF p_is_active = false AND p_actor_id IS NOT NULL AND p_actor_id = p_user_id THEN
        RETURN jsonb_build_object('error', 'You cannot deactivate yourself');
    END IF;

    -- Guard (moved from the route): never remove the last active admin.
    IF p_is_active = false AND v_current_active AND v_current_role = 'tenant_admin' THEN
        SELECT count(*)
        INTO v_active_admins
        FROM user_roles ur
        JOIN roles r ON r.id = ur.role_id
        JOIN users u ON u.id = ur.user_id
        WHERE ur.tenant_id = p_tenant_id
          AND r.name = 'tenant_admin'
          AND u.is_active = true;

        IF v_active_admins <= 1 THEN
            RETURN jsonb_build_object('error', 'Cannot deactivate the last active admin');
        END IF;
    END IF;

    -- Validate the target role before touching anything.
    IF p_role IS NOT NULL AND p_role IS DISTINCT FROM v_current_role THEN
        SELECT id
        INTO v_role_id
        FROM roles
        WHERE (tenant_id::text = p_tenant_id::text OR tenant_id IS NULL) AND name = p_role
        ORDER BY tenant_id NULLS LAST
        LIMIT 1;

        IF v_role_id IS NULL THEN
            RETURN jsonb_build_object('error', 'Invalid role: ' || p_role);
        END IF;
    END IF;

    -- Update is_active; audit only when the value actually changes.
    IF p_is_active IS NOT NULL AND p_is_active IS DISTINCT FROM v_current_active THEN
        UPDATE users SET is_active = p_is_active WHERE id = p_user_id::text;

        PERFORM record_auth_event(
            gen_random_uuid()::text,
            p_tenant_id::text,
            p_user_id::text,
            CASE WHEN p_is_active THEN 'user.activated' ELSE 'user.deactivated' END,
            'user',
            p_user_id::text,
            jsonb_build_object(
                'previous_is_active', v_current_active,
                'new_is_active', p_is_active,
                'actor_id', p_actor_id::text
            ),
            gen_random_uuid()::text
        );
    END IF;

    -- Update role (tenant-specific first, then global); audit the change.
    IF v_role_id IS NOT NULL THEN
        UPDATE user_roles
        SET role_id = v_role_id
        WHERE user_id = p_user_id::text AND tenant_id = p_tenant_id;

        PERFORM record_auth_event(
            gen_random_uuid()::text,
            p_tenant_id::text,
            p_user_id::text,
            'role.assigned',
            'user',
            p_user_id::text,
            jsonb_build_object(
                'previous_role', v_current_role,
                'new_role', p_role,
                'actor_id', p_actor_id::text
            ),
            gen_random_uuid()::text
        );
    END IF;

    -- Return updated member
    RETURN (
        SELECT coalesce(jsonb_build_object(
            'id',         u.id,
            'email',      u.email,
            'role',       r.name,
            'is_active',  u.is_active,
            'last_login', u.last_login,
            'created_at', u.created_at,
            'first_name', u.first_name,
            'last_name',  u.last_name
        ), '{}'::jsonb)
        FROM users u
        JOIN user_roles ur ON ur.user_id = u.id AND ur.tenant_id = p_tenant_id
        JOIN roles r ON r.id = ur.role_id
        WHERE u.id = p_user_id::text
    );
END;
$$;
"""


# Restore the exact 0037 definition (pre-audit) on downgrade.
_UPDATE_TENANT_MEMBER_0037 = """
DROP FUNCTION IF EXISTS public.update_tenant_member(UUID, UUID, TEXT, BOOLEAN, UUID);

CREATE OR REPLACE FUNCTION public.update_tenant_member(
    p_user_id   UUID,
    p_tenant_id UUID,
    p_role      TEXT DEFAULT NULL,
    p_is_active BOOLEAN DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_current_role TEXT;
    v_role_id      UUID;
BEGIN
    SELECT r.name INTO v_current_role
    FROM user_roles ur
    JOIN roles r ON r.id = ur.role_id
    WHERE ur.user_id = p_user_id AND ur.tenant_id = p_tenant_id;

    IF v_current_role IS NULL THEN
        RETURN jsonb_build_object('error', 'User not found in this tenant');
    END IF;

    IF p_is_active IS NOT NULL THEN
        UPDATE users SET is_active = p_is_active WHERE id = p_user_id;
    END IF;

    IF p_role IS NOT NULL AND p_role IS DISTINCT FROM v_current_role THEN
        SELECT id INTO v_role_id
        FROM roles
        WHERE (tenant_id::text = p_tenant_id::text OR tenant_id IS NULL) AND name = p_role
        ORDER BY tenant_id NULLS LAST LIMIT 1;

        IF v_role_id IS NULL THEN
            RETURN jsonb_build_object('error', 'Invalid role: ' || p_role);
        END IF;

        UPDATE user_roles
        SET role_id = v_role_id
        WHERE user_id = p_user_id AND tenant_id = p_tenant_id;
    END IF;

    RETURN (
        SELECT coalesce(jsonb_build_object(
            'id',         u.id,
            'email',      u.email,
            'role',       r.name,
            'is_active',  u.is_active,
            'last_login', u.last_login,
            'created_at', u.created_at,
            'first_name', u.first_name,
            'last_name',  u.last_name
        ), '{}'::jsonb)
        FROM users u
        JOIN user_roles ur ON ur.user_id = u.id AND ur.tenant_id = p_tenant_id
        JOIN roles r ON r.id = ur.role_id
        WHERE u.id = p_user_id
    );
END;
$$;
"""


def upgrade() -> None:
    op.execute(_UPDATE_TENANT_MEMBER_FN)


def downgrade() -> None:
    op.execute(_UPDATE_TENANT_MEMBER_0037)
