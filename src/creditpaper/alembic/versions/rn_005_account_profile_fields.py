"""Expose auth.users.created_at through get_supabase_auth_context.

Revision ID: rn_005_account_profile_fields
Revises: rn_004_same_domain_invites

Finding: GET /api/auth/me (and the /session bootstrap that seeds the same
cache) omit ``auth_provider`` and ``created_at``, while AccountTab declares
both on its Account type (account.ts:18-19):

  - AccountTab.tsx:248 gates the Change Password button on
    ``auth_provider === "email"`` -> undefined never matches -> the button
    never renders for email users,
  - AccountTab.tsx:280 runs ``new Date(created_at)`` -> Invalid Date for
    "Member Since".

``auth_provider`` is already on CurrentUser and just needs emitting from
``_profile_response``. ``created_at`` is nowhere upstream: no SP returned
it. This migration adds it to ``get_supabase_auth_context`` (auth.users =
the authoritative signup date); dependencies.py and _profile_response
carry it the rest of the way in the same change.

The function is LANGUAGE sql, so Postgres validates the body at CREATE
time and a bare local database (no auth schema yet — the test conftest
creates the stub after migrations run) would refuse it. Same
``check_function_bodies`` wrap as 0037. The 0037 body is carried verbatim
as the downgrade pre-image.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "rn_005_account_profile_fields"
down_revision: str | None = "rn_004_same_domain_invites"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AUTH_CONTEXT_FN = """
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
        'created_at', u.created_at,
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

# 0037 pre-image — identical except no created_at key.
_AUTH_CONTEXT_FN_PRE_IMAGE = """
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


def upgrade() -> None:
    op.execute("SET LOCAL check_function_bodies = false")
    op.execute(_AUTH_CONTEXT_FN)
    op.execute("SET LOCAL check_function_bodies = true")


def downgrade() -> None:
    op.execute("SET LOCAL check_function_bodies = false")
    op.execute(_AUTH_CONTEXT_FN_PRE_IMAGE)
    op.execute("SET LOCAL check_function_bodies = true")
