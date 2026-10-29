"""Invitations are same-domain only: reject invitees outside the tenant domain.

Revision ID: rn_004_same_domain_invites
Revises: rn_003_signup_honours_invite

Finding: POST /api/tenant/invite accepted any non-personal email regardless
of domain (admin@dell.com could invite uday@cylsy.com) because no layer in
the chain compared the invitee's domain to the tenant's domain:

  - the UI hint claims "Must be a @dell.com email address"
    (InviteUser.tsx) but getEmailError() never compares domains,
  - the route only checks role + non-empty email,
  - create_tenant_invitation only checks tenant exists + no duplicate,
  - the invitations table has no CHECK constraint.

Enforcement belongs in the SP (CLAUDE.md: no business logic in Python
routes); the route already surfaces SP ``error`` payloads as HTTP 400.
``create_tenant_invitation`` now validates, before the duplicate check:

  1. the email must contain ``@`` with a non-empty domain part
     -> ``Invalid email address``,
  2. the domain part must equal the tenant's domain (``domains.domain``
     for the resolved domain id, matched case-insensitively)
     -> ``Invitations can only be sent to @<domain> addresses``.

This also subsumes the personal-domain gap at the API layer: a gmail
invitee can never equal the tenant's domain, so it is rejected here even
though the route has no PUBLIC_DOMAINS check.

The 0037 body is carried verbatim as the downgrade pre-image.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "rn_004_same_domain_invites"
down_revision: str | None = "rn_003_signup_honours_invite"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
    v_domain_id      TEXT;
    v_email          TEXT;
    v_invite_domain  TEXT;
    v_tenant_domain  TEXT;
    v_existing       INT;
    v_inv            invitations%ROWTYPE;
BEGIN
    -- Resolve domain from tenant_id (handles both old and new tenants)
    v_domain_id := resolve_tenant_domain_id(p_tenant_id);

    IF v_domain_id IS NULL THEN
        RETURN jsonb_build_object('error', 'Tenant not found');
    END IF;

    v_email := lower(btrim(coalesce(p_email, '')));

    -- The address must have a domain part before we can compare it.
    IF v_email = '' OR position('@' in v_email) = 0
       OR btrim(split_part(v_email, '@', 2)) = '' THEN
        RETURN jsonb_build_object('error', 'Invalid email address');
    END IF;

    v_invite_domain := btrim(split_part(v_email, '@', 2));

    SELECT lower(d.domain) INTO v_tenant_domain
    FROM domains d
    WHERE d.id::text = v_domain_id::text;

    IF v_tenant_domain IS NOT NULL AND v_invite_domain <> v_tenant_domain THEN
        RETURN jsonb_build_object(
            'error',
            'Invitations can only be sent to @' || v_tenant_domain || ' addresses'
        );
    END IF;

    -- Check for existing pending invitation for same email + domain
    SELECT count(*) INTO v_existing
    FROM invitations i
    WHERE i.domain_id::text = v_domain_id::text
      AND lower(i.email) = v_email
      AND i.accepted = false;

    IF v_existing > 0 THEN
        RETURN jsonb_build_object('error', 'A pending invitation already exists for this email');
    END IF;

    -- Insert invitation with 7-day expiry
    INSERT INTO invitations (id, email, domain_id, role, token, expires_at, accepted, created_at)
    VALUES (
        gen_random_uuid()::text,
        v_email,
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


# Exact 0037 definition (pre-domain-check) for downgrade.
_CREATE_TENANT_INVITATION_0037 = """
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


def upgrade() -> None:
    op.execute(_CREATE_TENANT_INVITATION_FN)


def downgrade() -> None:
    op.execute(_CREATE_TENANT_INVITATION_0037)
