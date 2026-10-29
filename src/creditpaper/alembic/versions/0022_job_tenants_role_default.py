"""worker tenant discovery and the maker role default.

Revision ID: 0022_job_tenants_role_default
Revises: 0021_pin_function_search_path

(a) `processing_job_tenants()`: `processing_jobs` has forced RLS on `app.tenant_id`, so a
worker serving every tenant cannot see which tenants have work without a tenant set. The
function runs as its owner (SECURITY DEFINER, pinned search_path) and returns tenant ids
only: no job ids, kinds, payloads or results. The worker then claims each tenant's jobs
through the normal tenant cursor, under RLS. Executable by the runtime role only.

(b) New `users` rows default to `maker` (ruled 2026-09-15); `user` no longer grants access.

ADDITIVE ONLY. Offline-safe: no reads from Python; role checks run inside the database.
Downgrade drops the function and restores the `user` default.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022_job_tenants_role_default"
down_revision: str | None = "0021_pin_function_search_path"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION public.processing_job_tenants() RETURNS SETOF uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT DISTINCT tenant_id FROM processing_jobs
          WHERE status = 'pending' OR (status = 'running' AND lease_until < clock_timestamp())
        $$
        """
    )
    op.execute("REVOKE EXECUTE ON FUNCTION public.processing_job_tenants() FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE EXECUTE ON FUNCTION public.processing_job_tenants() FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE EXECUTE ON FUNCTION public.processing_job_tenants() FROM authenticated;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditpaper_runtime') THEN
            GRANT EXECUTE ON FUNCTION public.processing_job_tenants() TO creditpaper_runtime;
          END IF;
        END $$
        """
    )
    # processing_jobs has FORCE ROW LEVEL SECURITY, which binds the table owner too. The
    # function sees rows only when its owner (the migrating role) bypasses RLS; say so
    # at apply time rather than let the worker find no work in silence.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user
                         AND (rolsuper OR rolbypassrls)) THEN
            RAISE WARNING 'processing_job_tenants() owner % does not bypass RLS; %',
              current_user, 'the all-tenants worker will find no jobs';
          END IF;
        END $$
        """
    )
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'maker'")


def downgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'user'")
    op.execute("DROP FUNCTION IF EXISTS public.processing_job_tenants()")
