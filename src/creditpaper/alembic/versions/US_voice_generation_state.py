"""Voice generation status moves from process memory to the database.

Status must survive restarts and be correct across workers: the job runs in a
daemon thread in one process while /status may be answered by another.
"""

from alembic import op

revision = "US_voice_generation_state"
down_revision = "US_profile_created_at"
branch_labels = None
depends_on = None

_TABLE = """
    CREATE TABLE IF NOT EXISTS voice_generation_state (
        tenant_id  VARCHAR PRIMARY KEY,
        status     VARCHAR NOT NULL CHECK (status IN ('generating','done','error')),
        job_id     VARCHAR,
        error      TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
"""

# Not FORCE RLS: sibling onboarding tables (onboarding_documents, voice_profile)
# run without it and the voice routes do not set app.tenant_id; repo-wide RLS
# enforcement is a separate initiative. tenant_id is still on the table.
_FUNCTIONS = """
    CREATE OR REPLACE FUNCTION public.claim_voice_generation_job(
        p_tenant_id VARCHAR, p_job_id VARCHAR
    ) RETURNS BOOLEAN LANGUAGE sql AS $fn$
        WITH claimed AS (
            INSERT INTO voice_generation_state (tenant_id, status, job_id, error, updated_at)
            VALUES (p_tenant_id, 'generating', p_job_id, NULL, now())
            ON CONFLICT (tenant_id) DO UPDATE
               SET status = 'generating', job_id = EXCLUDED.job_id,
                   error = NULL, updated_at = now()
             WHERE voice_generation_state.status IS DISTINCT FROM 'generating'
                OR voice_generation_state.updated_at < now() - interval '15 minutes'
            RETURNING 1
        ) SELECT count(*) > 0 FROM claimed
    $fn$;

    CREATE OR REPLACE FUNCTION public.set_voice_generation_state(
        p_tenant_id VARCHAR, p_status VARCHAR, p_job_id VARCHAR, p_error TEXT DEFAULT NULL
    ) RETURNS VOID LANGUAGE sql AS $fn$
        INSERT INTO voice_generation_state (tenant_id, status, job_id, error, updated_at)
        VALUES (p_tenant_id, p_status, p_job_id, p_error, now())
        ON CONFLICT (tenant_id) DO UPDATE
           SET status = EXCLUDED.status, job_id = EXCLUDED.job_id,
               error = EXCLUDED.error, updated_at = now()
    $fn$;

    CREATE OR REPLACE FUNCTION public.get_voice_generation_state(
        p_tenant_id VARCHAR
    ) RETURNS JSONB LANGUAGE sql AS $fn$
        SELECT jsonb_build_object(
            'status', status, 'job_id', job_id, 'error', error,
            'updated_at', to_char(updated_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
        )
        FROM voice_generation_state WHERE tenant_id = p_tenant_id
    $fn$;

    CREATE OR REPLACE FUNCTION public.clear_voice_generation_state(
        p_tenant_id VARCHAR
    ) RETURNS VOID LANGUAGE sql AS $fn$
        DELETE FROM voice_generation_state WHERE tenant_id = p_tenant_id
    $fn$;
"""


def upgrade() -> None:
    op.execute(_TABLE)
    op.execute(_FUNCTIONS)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.clear_voice_generation_state(VARCHAR)")
    op.execute("DROP FUNCTION IF EXISTS public.get_voice_generation_state(VARCHAR)")
    op.execute("DROP FUNCTION IF EXISTS public.set_voice_generation_state(VARCHAR, VARCHAR, VARCHAR, TEXT)")
    op.execute("DROP FUNCTION IF EXISTS public.claim_voice_generation_job(VARCHAR, VARCHAR)")
    op.execute("DROP TABLE IF EXISTS voice_generation_state")
