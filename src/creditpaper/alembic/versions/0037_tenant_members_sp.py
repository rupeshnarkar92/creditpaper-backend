"""Tenant members and invitations stored procedures.

Deploy before the application callers. Each function returns JSONB so the
Python side deserialises in one round-trip.

Functions added:
  - resolve_tenant_domain_id(p_tenant_id)          -- helper: find domains.id for a tenant
  - get_tenant_members(p_tenant_id)                -- list members with roles
  - get_tenant_invitations(p_tenant_id)            -- list pending/expired invitations
  - create_tenant_invitation(p_tenant_id, ...)     -- create invitation (7-day expiry)
  - resend_tenant_invitation(p_invitation_id, ...) -- reset expiry to +7 days
  - cancel_tenant_invitation(p_invitation_id, ...) -- delete invitation
  - update_tenant_member(p_user_id, ...)           -- update role or is_active
"""

from alembic import op

revision = "0037_tenant_members_sp"
down_revision = "0036_workspace_read_indexes"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# resolve_tenant_domain_id — helper to find domains.id for a tenant
# Handles two cases:
#   1. New tenants: domains.id = tenants.id (same UUID, created by provisioning)
#   2. Old tenants: match via tenants.email_domain = domains.domain
# ---------------------------------------------------------------------------

_RESOLVE_DOMAIN_ID_FN = """
CREATE OR REPLACE FUNCTION resolve_tenant_domain_id(p_tenant_id UUID)
RETURNS TEXT
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    -- Case 1: direct ID match (new tenants — provisioning creates both rows with same UUID)
    SELECT d.id::text FROM domains d WHERE d.id::text = p_tenant_id::text
    UNION ALL
    -- Case 2: match via email_domain (old tenants — domains created before provisioning fix)
    SELECT d.id::text FROM domains d
    JOIN tenants t ON lower(t.email_domain) = lower(d.domain)
    WHERE t.id::text = p_tenant_id::text
      AND NOT EXISTS (SELECT 1 FROM domains d2 WHERE d2.id::text = p_tenant_id::text)
    LIMIT 1;
$$;
"""


# ---------------------------------------------------------------------------
# get_tenant_members — list all users in a tenant with their roles
# ---------------------------------------------------------------------------

_GET_TENANT_MEMBERS_FN = """
CREATE OR REPLACE FUNCTION get_tenant_members(p_tenant_id UUID)
RETURNS JSONB
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'id',         u.id,
            'email',      u.email,
            'role',       r.name,
            'is_active',  u.is_active,
            'last_login', u.last_login,
            'created_at', u.created_at,
            'first_name', u.first_name,
            'last_name',  u.last_name
        ) ORDER BY u.created_at
    ), '[]'::jsonb)
    FROM user_roles ur
    JOIN users u ON u.id = ur.user_id
    JOIN roles r ON r.id = ur.role_id
    WHERE ur.tenant_id = p_tenant_id;
$$;
"""


# ---------------------------------------------------------------------------
# get_tenant_invitations — list pending/expired invitations for this tenant
# ---------------------------------------------------------------------------

_GET_TENANT_INVITATIONS_FN = """
CREATE OR REPLACE FUNCTION get_tenant_invitations(p_tenant_id UUID)
RETURNS JSONB
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'id',         i.id,
            'email',      i.email,
            'role',       i.role,
            'expires_at', i.expires_at,
            'accepted',   i.accepted,
            'created_at', i.created_at
        ) ORDER BY i.created_at DESC
    ), '[]'::jsonb)
    FROM invitations i
    WHERE i.domain_id::text = resolve_tenant_domain_id(p_tenant_id)::text
      AND i.accepted = false;
$$;
"""


# ---------------------------------------------------------------------------
# create_tenant_invitation — insert a new invitation with 7-day expiry
# ---------------------------------------------------------------------------

_CREATE_TENANT_INVITATION_FN = """
CREATE OR REPLACE FUNCTION create_tenant_invitation(
    p_tenant_id UUID,
    p_email     TEXT,
    p_role      TEXT,
    p_token     TEXT
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_domain_id TEXT;
    v_existing  INT;
    v_inv       invitations%ROWTYPE;
BEGIN
    -- Resolve domain from tenant_id (handles both old and new tenants)
    v_domain_id := resolve_tenant_domain_id(p_tenant_id);

    IF v_domain_id IS NULL THEN
        RETURN jsonb_build_object('error', 'Tenant not found');
    END IF;

    -- Check for existing pending invitation for same email + domain
    SELECT count(*) INTO v_existing
    FROM invitations i
    WHERE i.domain_id::text = v_domain_id::text
      AND lower(i.email) = lower(p_email)
      AND i.accepted = false;

    IF v_existing > 0 THEN
        RETURN jsonb_build_object('error', 'A pending invitation already exists for this email');
    END IF;

    -- Insert invitation with 7-day expiry
    INSERT INTO invitations (id, email, domain_id, role, token, expires_at, accepted, created_at)
    VALUES (
        gen_random_uuid()::text,
        lower(p_email),
        v_domain_id::text,
        p_role,
        p_token,
        now() + interval '7 days',
        false,
        now()
    )
    RETURNING * INTO v_inv;

    RETURN jsonb_build_object(
        'id',         v_inv.id,
        'email',      v_inv.email,
        'role',       v_inv.role,
        'expires_at', v_inv.expires_at,
        'accepted',   v_inv.accepted,
        'created_at', v_inv.created_at
    );
END;
$$;
"""


# ---------------------------------------------------------------------------
# resend_tenant_invitation — reset expiry to +7 days
# ---------------------------------------------------------------------------

_RESEND_TENANT_INVITATION_FN = """
CREATE OR REPLACE FUNCTION resend_tenant_invitation(
    p_invitation_id UUID,
    p_tenant_id     UUID
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_domain_id TEXT;
    v_inv       invitations%ROWTYPE;
BEGIN
    v_domain_id := resolve_tenant_domain_id(p_tenant_id);

    UPDATE invitations i
    SET expires_at = now() + interval '7 days'
    WHERE i.id::text = p_invitation_id::text
      AND i.domain_id::text = v_domain_id::text
      AND i.accepted = false
    RETURNING i.* INTO v_inv;

    IF v_inv.id IS NULL THEN
        RETURN jsonb_build_object('error', 'Invitation not found');
    END IF;

    RETURN jsonb_build_object(
        'id',         v_inv.id,
        'email',      v_inv.email,
        'role',       v_inv.role,
        'expires_at', v_inv.expires_at,
        'accepted',   v_inv.accepted,
        'created_at', v_inv.created_at
    );
END;
$$;
"""


# ---------------------------------------------------------------------------
# cancel_tenant_invitation — delete an invitation
# ---------------------------------------------------------------------------

_CANCEL_TENANT_INVITATION_FN = """
CREATE OR REPLACE FUNCTION cancel_tenant_invitation(
    p_invitation_id UUID,
    p_tenant_id     UUID
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_domain_id TEXT;
BEGIN
    v_domain_id := resolve_tenant_domain_id(p_tenant_id);

    DELETE FROM invitations i
    WHERE i.id::text = p_invitation_id::text
      AND i.domain_id::text = v_domain_id::text;

    RETURN jsonb_build_object('deleted', FOUND);
END;
$$;
"""


# ---------------------------------------------------------------------------
# update_tenant_member — update role or is_active
# ---------------------------------------------------------------------------

_UPDATE_TENANT_MEMBER_FN = """
CREATE OR REPLACE FUNCTION update_tenant_member(
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
    -- Verify user exists in this tenant
    SELECT r.name INTO v_current_role
    FROM user_roles ur
    JOIN roles r ON r.id = ur.role_id
    WHERE ur.user_id = p_user_id AND ur.tenant_id = p_tenant_id;

    IF v_current_role IS NULL THEN
        RETURN jsonb_build_object('error', 'User not found in this tenant');
    END IF;

    -- Update is_active on users table
    IF p_is_active IS NOT NULL THEN
        UPDATE users SET is_active = p_is_active WHERE id = p_user_id;
    END IF;

    -- Update role on user_roles table (tenant-specific first, then global)
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
        WHERE u.id = p_user_id
    );
END;
$$;
"""


# ---------------------------------------------------------------------------
# Downgrade — drop all functions
# ---------------------------------------------------------------------------

_DOWNGRADE_FNS = """
DROP FUNCTION IF EXISTS resolve_tenant_domain_id(UUID);
DROP FUNCTION IF EXISTS get_tenant_members(UUID);
DROP FUNCTION IF EXISTS get_tenant_invitations(UUID);
DROP FUNCTION IF EXISTS create_tenant_invitation(UUID, TEXT, TEXT, TEXT);
DROP FUNCTION IF EXISTS resend_tenant_invitation(UUID, UUID);
DROP FUNCTION IF EXISTS cancel_tenant_invitation(UUID, UUID);
DROP FUNCTION IF EXISTS update_tenant_member(UUID, UUID, TEXT, BOOLEAN);
"""


def upgrade() -> None:
    op.execute(_RESOLVE_DOMAIN_ID_FN)

    # Production's domains table carried company/status from the pre-Alembic
    # shim; a fresh replay of 0002_auth_tables creates only
    # (id, domain, admin_email, created_at). Add the two columns here —
    # right where they are first needed — so fresh builds match production.
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS company VARCHAR")
    op.execute("ALTER TABLE domains ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'active'")

    # Backfill: create domains rows for existing tenants that don't have one.
    # This handles users who signed up before provisioning.py was updated to
    # also insert into domains.
    op.execute(
        """
        INSERT INTO domains (id, domain, admin_email, company, status)
        SELECT t.id::text, t.email_domain, COALESCE(t.email_domain, 'unknown'),
               t.name, COALESCE(t.status, 'active')
        FROM tenants t
        WHERE t.email_domain IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM domains d WHERE d.id::text = t.id::text)
        ON CONFLICT (domain) DO NOTHING
        """
    )

    op.execute(_GET_TENANT_MEMBERS_FN)
    op.execute(_GET_TENANT_INVITATIONS_FN)
    op.execute(_CREATE_TENANT_INVITATION_FN)
    op.execute(_RESEND_TENANT_INVITATION_FN)
    op.execute(_CANCEL_TENANT_INVITATION_FN)
    op.execute(_UPDATE_TENANT_MEMBER_FN)


def downgrade() -> None:
    op.execute(_DOWNGRADE_FNS)
