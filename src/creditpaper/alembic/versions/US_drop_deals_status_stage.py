"""Drop the INSERT-time duplicate columns deals.status and deals.stage.

Revision ID: US_drop_deals_status_stage
Revises: US_merge_voice_nn0048

``status`` (0004) and ``stage`` (0001) were written once at INSERT and never
updated: the state machine writes ``current_state`` only (workspace.py
``SET current_state``). Both therefore read Draft forever. rn_009 stopped
``get_deals_list`` from feeding the board badge with ``status``; this migration
finishes the job and removes the columns themselves.

The columns were not merely idle - they were a trap. ``DealResponse`` had to
carry a comment saying "do not use it to display where a deal is", and any
future ``to_jsonb(d)`` would silently ship ``'draft'`` for a deal in any state,
which is how the stuck-badge bug was born in the first place.

Both stored procedures are patched in place with the ``pg_get_functiondef`` +
``replace`` + ``EXECUTE`` technique 0033/0034/0035/0038/rn_008/rn_009 already
established, so their bodies are not duplicated here. Each patch raises when its
anchor string is absent, so a definition that drifts fails the migration loudly
rather than silently shipping a body that still names a dropped column.

- ``get_deals_list`` - drops ``stage`` and ``status`` from the CTE and drops the
  explicit ``'status'`` key the JSON assembly built from ``d.status``.
- ``get_workspace_payload`` - drops ``status`` from the deal subquery.

Order is the whole game and mirrors rn_008's inverse rule ("column first: this is
LANGUAGE sql and is parsed when created"):

- upgrade - patch the bodies first, drop the columns last, so no definition ever
  names a column that is gone.
- downgrade - re-add the columns first, restore the references last.

``ix_deals_status`` and ``ix_deals_tenant_status`` (both 0004) depend on
``status`` and go with it; downgrade recreates them verbatim. ``stage`` was plain
nullable text with no default and no index. Neither column carries a foreign key
or a check constraint, so nothing else in the schema depends on them.

Downgrade cannot recover the dropped *values*: ``status`` comes back populated
from its ``DEFAULT 'draft'`` (correct for every row, since INSERT wrote nothing
else), but ``stage`` returns NULL rather than its previous ``'draft'``. That is
the honest result - the values were destroyed on upgrade by definition - and it
is safe precisely because nothing reads either column. Verified by a live
downgrade/upgrade round trip on the dev database.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "US_drop_deals_status_stage"
down_revision: str | None = "US_merge_voice_nn0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (signature, find, replace). ``find`` must occur exactly once in that signature's
# body - the first entry deliberately spans two lines so that its reverse (whose
# anchor is the surviving ``'document_required'``) is non-empty, because an empty
# anchor cannot be located by position().
#
# ORDER IS LOAD-BEARING. Each patch is applied and EXECUTEd on its own, so every
# intermediate body has to compile on its own:
#   1. drop the ``'status'`` key that reads ``d.status`` first, or step 2 leaves a
#      reference to a column the CTE no longer selects (LANGUAGE sql is parsed at
#      CREATE, so it fails immediately rather than at first call);
#   2. then drop ``status`` from the CTE;
#   3. ``stage`` last - nothing in the body ever reads it.
# downgrade walks the same list in reverse, which lands back on valid bodies too.
#
# Single quotes are doubled by _quote() before they reach the SQL literal; newlines
# are legal inside one.
_PATCHES: tuple[tuple[str, str, str], ...] = (
    (
        "public.get_deals_list(uuid)",
        "'status', coalesce(nullif(d.status, ''), 'draft'),\n            'document_required'",
        "'document_required'",
    ),
    (
        "public.get_deals_list(uuid)",
        "facility_amount, status, current_state, document_count, document_required,",
        "facility_amount, current_state, document_count, document_required,",
    ),
    (
        "public.get_deals_list(uuid)",
        "stage, thesis_content, thesis_status, owner_id,",
        "thesis_content, thesis_status, owner_id,",
    ),
    (
        "public.get_workspace_payload(uuid,text)",
        "facility_amount, status, updated_at",
        "facility_amount, updated_at",
    ),
)


def _quote(text: str) -> str:
    """Escape a value for embedding in a SQL single-quoted literal inside the DO block."""
    return text.replace("'", "''")


def _patch(signature: str, find: str, to: str) -> None:
    find_sql = _quote(find)
    to_sql = _quote(to)
    # No % format specifiers in the RAISE: psycopg treats % as a parameter marker,
    # so a guard that fired would fail on formatting instead of naming the anchor.
    op.execute(
        f"""
        DO $$
        DECLARE definition TEXT;
        BEGIN
            SELECT pg_get_functiondef('{signature}'::regprocedure) INTO definition;
            IF position('{find_sql}' IN definition) = 0 THEN
                RAISE EXCEPTION USING
                    MESSAGE = 'deals.status/stage anchor missing from {signature}: '
                              || left('{find_sql}', 70);
            END IF;
            definition := replace(definition, '{find_sql}', '{to_sql}');
            EXECUTE definition;
        END $$;
        """
    )


def upgrade() -> None:
    for signature, find, to in _PATCHES:
        _patch(signature, find, to)
    # Last: the definitions above are the only things still naming these columns.
    op.execute("ALTER TABLE deals DROP COLUMN IF EXISTS status")
    op.execute("ALTER TABLE deals DROP COLUMN IF EXISTS stage")


def downgrade() -> None:
    # First: get_deals_list is LANGUAGE sql and is parsed when created, so the
    # columns have to exist before a body can name them.
    op.execute("ALTER TABLE deals ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft'")
    op.execute("ALTER TABLE deals ADD COLUMN IF NOT EXISTS stage TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS ix_deals_status ON deals (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_deals_tenant_status ON deals (tenant_id, status)")
    for signature, find, to in reversed(_PATCHES):
        _patch(signature, to, find)
