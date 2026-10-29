"""Expose auth.users.created_at through the auth context, for GET /api/auth/me.

The Settings > Account tab renders "Member Since" from /me, but the payload
never carried a creation date (it showed "Invalid Date"). One field added to
the existing SP; signature and grants unchanged (CREATE OR REPLACE).
"""

from alembic import op

revision = "US_profile_created_at"
down_revision = "rn_005_account_profile_fields"
branch_labels = None
depends_on = None

# Body copied verbatim from 0037_auth_signup_stored_procedures._AUTH_CONTEXT
# with exactly one addition: the created_at field.
_AUTH_CONTEXT = """
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


def upgrade() -> None:
    op.execute(_AUTH_CONTEXT)


def downgrade() -> None:
    # Re-run the 0037 body (created_at removed) — same signature, no grant churn.
    op.execute(
        """
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
    )
