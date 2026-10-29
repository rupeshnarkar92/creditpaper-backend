"""MFA policy admin control: read and set a firm's two-factor requirement.

Revision ID: rn_007_mfa_policy_admin
Revises: rn_006_mfa_core

AUTH-11 slice P2. rn_006 shipped the ``tenants.require_mfa`` switch and its
enforcement, but nothing could change it: enforcement was dormant until someone
edited the column by hand. This migration adds the two stored procedures the admin
screen calls, so the policy is a decision a firm makes in the product.

- ``get_tenant_mfa_policy(p_tenant_id)`` — the switch plus how many of the firm's
  active members have set two-factor up. The counts are the actionable part: turning
  the policy on is what forces the stragglers at their next password sign-in.
- ``set_tenant_mfa_policy(p_tenant_id, p_require_mfa)`` — the only writer of
  ``tenants.require_mfa``, and it returns the read shape so the admin screen never
  has to follow a write with a read.

Both are tenant-scoped parameters, never a body field: the API resolves the tenant
from the signed-in session and passes ``user.tenant_id``, so a client cannot name a
firm it does not belong to. Authorization (``settings.update``, i.e. ``tenant_admin``)
is enforced at the route, and the caller is recorded in the auth audit trail.

plpgsql throughout; GRANT/REVOKE follows rn_006 (``creditpaper_runtime`` only, PUBLIC
and the PostgREST roles revoked).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "rn_007_mfa_policy_admin"
down_revision: str | None = "rn_006_mfa_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_GET_TENANT_MFA_POLICY = """
CREATE OR REPLACE FUNCTION public.get_tenant_mfa_policy(
    p_tenant_id TEXT
) RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, auth, pg_temp
AS $$
DECLARE
    v_require_mfa BOOLEAN := FALSE;
    v_members INTEGER := 0;
    v_enrolled INTEGER := 0;
BEGIN
    SELECT coalesce(t.require_mfa, FALSE)
      INTO v_require_mfa
      FROM public.tenants t
     WHERE t.id::text = p_tenant_id;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', FALSE, 'error', 'TENANT_NOT_FOUND');
    END IF;

    -- One row per active member: a person holding two roles in the firm counts once,
    -- and a deactivated account is not someone the policy can strand
    -- (public.users.is_active is the flag; deactivation is how a leaver is removed).
    SELECT count(*), count(*) FILTER (WHERE u.mfa_enrolled_at IS NOT NULL)
      INTO v_members, v_enrolled
      FROM (
          SELECT DISTINCT u.id::text AS id, u.mfa_enrolled_at
            FROM public.users u
            JOIN public.user_roles r ON r.user_id = u.id::text
           WHERE r.tenant_id::text = p_tenant_id
             AND u.is_active
      ) u;

    RETURN jsonb_build_object(
        'ok', TRUE,
        'require_mfa', v_require_mfa,
        'members', v_members,
        'enrolled', v_enrolled
    );
END;
$$;
"""


_SET_TENANT_MFA_POLICY = """
CREATE OR REPLACE FUNCTION public.set_tenant_mfa_policy(
    p_tenant_id TEXT,
    p_require_mfa BOOLEAN
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public, auth, pg_temp
AS $$
DECLARE
    v_previous BOOLEAN;
BEGIN
    SELECT t.require_mfa
      INTO v_previous
      FROM public.tenants t
     WHERE t.id::text = p_tenant_id
       AND t.status = 'active'
     FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', FALSE, 'error', 'TENANT_NOT_FOUND');
    END IF;

    UPDATE public.tenants
       SET require_mfa = coalesce(p_require_mfa, FALSE)
     WHERE id::text = p_tenant_id;

    RETURN public.get_tenant_mfa_policy(p_tenant_id)
        || jsonb_build_object('previous', coalesce(v_previous, FALSE));
END;
$$;
"""


_RESTRICT = """
DO $$
DECLARE
    v_signature TEXT;
    v_role TEXT;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'public.get_tenant_mfa_policy(TEXT)',
        'public.set_tenant_mfa_policy(TEXT, BOOLEAN)'
    ] LOOP
        EXECUTE 'REVOKE ALL ON FUNCTION ' || v_signature || ' FROM PUBLIC';
        FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
                EXECUTE 'REVOKE ALL ON FUNCTION ' || v_signature || ' FROM ' || quote_ident(v_role);
            END IF;
        END LOOP;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
            EXECUTE 'GRANT EXECUTE ON FUNCTION ' || v_signature || ' TO creditpaper_runtime';
        END IF;
    END LOOP;
END;
$$;
"""


def upgrade() -> None:
    op.execute(_GET_TENANT_MFA_POLICY)
    op.execute(_SET_TENANT_MFA_POLICY)
    op.execute(_RESTRICT)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.set_tenant_mfa_policy(TEXT, BOOLEAN)")
    op.execute("DROP FUNCTION IF EXISTS public.get_tenant_mfa_policy(TEXT)")
