"""SEC-14: drop the dead `get_login_user` function.

`get_login_user(TEXT)` is a thin `SELECT to_jsonb(u) FROM users u WHERE u.email
= p_email` wrapper. It has no caller left in the engine — password login is
Supabase's — and `to_jsonb(u)` serialises the entire users row,
`password_hash` included. Any role that can EXECUTE it can pull credential
hashes by email address, one row at a time.

The function was already revoked from PUBLIC, anon and authenticated, but the
revocation is only as good as the grant state of the deployment, and a
`SECURITY INVOKER` function readable by the backend role is a standing
foot-gun. Deleting it removes the capability rather than guarding it.

`finish_password_login` is untouched: it is still live, takes the hash as a
verified precondition rather than returning it, and stays revoked from PUBLIC.
"""

from alembic import op

revision = "nn_0048_drop_get_login_user"
down_revision = "nn_0047_job_retry_dead_letters"
branch_labels = None
depends_on = None


_LOGIN_USER_FN = """
CREATE OR REPLACE FUNCTION get_login_user(p_email TEXT)
RETURNS JSONB
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    SELECT to_jsonb(u) FROM users u WHERE u.email = p_email;
$$;
"""

_LOGIN_USER_REVOKE = """
REVOKE ALL ON FUNCTION public.get_login_user(TEXT) FROM PUBLIC;
DO $$
DECLARE
    v_role TEXT;
BEGIN
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
            EXECUTE format(
                'REVOKE ALL ON FUNCTION public.get_login_user(TEXT) FROM %I', v_role);
        END IF;
    END LOOP;
END;
$$;
"""


def upgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.get_login_user(TEXT)")


def downgrade() -> None:
    op.execute(_LOGIN_USER_FN)
    op.execute(_LOGIN_USER_REVOKE)
