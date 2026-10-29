"""Consolidate deals-list reads and password-login database operations.

Deploy before the application callers. Password verification stays in Python;
login therefore uses two function calls, plus the session commit.
"""

from alembic import op

revision = "0030_deals_login_functions"
down_revision = ("0028_stored_procedures", "0029_password_history")
branch_labels = None
depends_on = None


_DEALS_LIST_FN = """
CREATE OR REPLACE FUNCTION get_deals_list(p_tenant_id UUID)
RETURNS JSONB
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    WITH tenant_deals AS (
        SELECT id, tenant_id, name, borrower, deal_type, sector, address,
               stage, thesis_content, thesis_status, owner_id,
               facility_amount, status, document_count, document_required,
               created_at, updated_at, template_id, template_source, template_snapshot
        FROM deals WHERE tenant_id = p_tenant_id
    ), required_slots AS (
        SELECT d.id AS deal_id, st.key
        FROM tenant_deals d
        JOIN document_slot_types st
          ON ('all' = ANY(st.applies_to) OR d.deal_type = ANY(st.applies_to))
         AND NOT ('all' = ANY(st.optional_for) OR d.deal_type = ANY(st.optional_for))
        WHERE d.deal_type IN ('construction', 'bridge', 'residual_stock')
    ), confirmed_slots AS (
        SELECT s.deal_id, s.slot_key
        FROM document_slots s
        JOIN tenant_deals td ON td.id = s.deal_id
        JOIN document_slot_files f ON f.slot_id = s.id
          AND f.tenant_id = p_tenant_id AND f.role = 'assigned'
        JOIN deal_documents d ON d.id = f.document_id
          AND d.tenant_id = p_tenant_id AND d.deal_id = s.deal_id AND d.archived_at IS NULL
        JOIN deal_document_versions v ON v.id = f.version_id
          AND v.tenant_id = p_tenant_id AND v.document_id = d.id
        WHERE s.tenant_id = p_tenant_id AND s.status IN ('confirmed', 'processing')
        GROUP BY s.deal_id, s.slot_key, s.status
        HAVING s.status = 'confirmed' OR bool_and(v.extraction_status = 'ready')
    ), counts AS (
        SELECT r.deal_id, count(*) AS required, count(c.slot_key) AS confirmed
        FROM required_slots r
        LEFT JOIN confirmed_slots c ON c.deal_id = r.deal_id AND c.slot_key = r.key
        GROUP BY r.deal_id
    )
    SELECT coalesce(jsonb_agg(
        (to_jsonb(d) - 'template_snapshot') || jsonb_build_object(
            'status', coalesce(nullif(d.status, ''), 'draft'),
            'document_required', CASE WHEN d.deal_type IN ('construction', 'bridge', 'residual_stock')
                THEN coalesce(c.required, 0) ELSE coalesce(nullif(d.document_required, 0), 25) END,
            'document_count', CASE WHEN d.deal_type IN ('construction', 'bridge', 'residual_stock')
                THEN coalesce(c.confirmed, 0) ELSE coalesce(d.document_count, 0) END,
            'template_id', coalesce(d.template_id::text,
                CASE WHEN jsonb_typeof(d.template_snapshot) = 'object' THEN 'platform:standard' END),
            'template_name', CASE WHEN jsonb_typeof(d.template_snapshot) = 'object'
                THEN d.template_snapshot->>'name' END
        ) ORDER BY d.updated_at DESC NULLS LAST, d.created_at DESC, d.id
    ), '[]'::jsonb)
    FROM tenant_deals d LEFT JOIN counts c ON c.deal_id = d.id;
$$;
"""


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


_FINISH_LOGIN_FN = """
CREATE OR REPLACE FUNCTION finish_password_login(
    p_user_id TEXT,
    p_expected_hash TEXT,
    p_verified BOOLEAN,
    p_max_attempts INTEGER,
    p_lock_seconds INTEGER
) RETURNS JSONB
LANGUAGE plpgsql
VOLATILE
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_user users%ROWTYPE;
    v_now TIMESTAMPTZ;
    v_onboarding BOOLEAN;
BEGIN
    -- Only the trusted backend supplies the password-verification result.
    -- Lock after hashing, so Argon2 never holds a row lock. Recheck current
    -- credentials and account state before granting a session.
    SELECT * INTO v_user FROM users WHERE id = p_user_id FOR UPDATE;
    IF NOT FOUND OR v_user.password_hash IS DISTINCT FROM p_expected_hash THEN
        RETURN jsonb_build_object('status', 'INVALID_CREDENTIALS');
    END IF;
    v_now := clock_timestamp();
    IF v_user.locked_until > v_now THEN
        RETURN jsonb_build_object('status', 'ACCOUNT_LOCKED', 'locked_until', v_user.locked_until);
    END IF;

    IF p_verified IS NOT TRUE OR v_user.password_hash IS NULL THEN
        UPDATE users SET
            failed_login_attempts = coalesce(failed_login_attempts, 0) + 1,
            locked_until = CASE WHEN coalesce(failed_login_attempts, 0) + 1 >= p_max_attempts
                THEN v_now + make_interval(secs => p_lock_seconds) ELSE locked_until END,
            updated_at = v_now
        WHERE id = p_user_id RETURNING * INTO v_user;
        IF v_user.locked_until > v_now THEN
            RETURN jsonb_build_object('status', 'ACCOUNT_LOCKED', 'locked_until', v_user.locked_until);
        END IF;
        RETURN jsonb_build_object('status', 'INVALID_CREDENTIALS');
    END IF;

    IF v_user.is_active IS NOT TRUE THEN
        RETURN jsonb_build_object('status', 'ACCOUNT_DEACTIVATED');
    END IF;
    IF v_user.auth_provider = 'email' AND v_user.is_email_verified IS NOT TRUE THEN
        RETURN jsonb_build_object('status', 'EMAIL_NOT_VERIFIED');
    END IF;

    UPDATE users SET failed_login_attempts = 0, locked_until = NULL,
        last_login = v_now, updated_at = v_now
    WHERE id = p_user_id RETURNING * INTO v_user;

    IF v_user.role = 'tenant_admin' AND v_user.is_super_admin IS NOT TRUE THEN
        SELECT EXISTS (
            SELECT 1 FROM onboarding_documents
            WHERE tenant_id = v_user.domain_id::uuid
              AND status = 'validated' AND approval_detected = true
        ) INTO v_onboarding;
    END IF;
    RETURN jsonb_build_object('status', 'OK', 'user', to_jsonb(v_user) - 'password_hash',
                              'onboarding_completed', v_onboarding);
END;
$$;
"""


_LOGIN_PRIVILEGES_SQL = """
REVOKE ALL ON FUNCTION get_login_user(TEXT),
    finish_password_login(TEXT, TEXT, BOOLEAN, INTEGER, INTEGER) FROM PUBLIC;
DO $$
DECLARE
    v_role TEXT;
BEGIN
    -- Supabase may grant API roles EXECUTE through default privileges.
    -- Login functions belong to the trusted backend, never browser RPC clients.
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
            EXECUTE format(
                'REVOKE ALL ON FUNCTION get_login_user(TEXT), '
                'finish_password_login(TEXT, TEXT, BOOLEAN, INTEGER, INTEGER) FROM %I', v_role);
        END IF;
    END LOOP;
END;
$$;
"""


def upgrade() -> None:
    op.execute(_DEALS_LIST_FN)
    op.execute(_LOGIN_USER_FN)
    op.execute(_FINISH_LOGIN_FN)
    op.execute(_LOGIN_PRIVILEGES_SQL)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS finish_password_login(TEXT, TEXT, BOOLEAN, INTEGER, INTEGER)")
    op.execute("DROP FUNCTION IF EXISTS get_login_user(TEXT)")
    op.execute("DROP FUNCTION IF EXISTS get_deals_list(UUID)")
