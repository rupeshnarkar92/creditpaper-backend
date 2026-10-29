"""Expose deals.current_state from get_deals_list.

The deals board's "Stage" badge read `status`, which is written once at INSERT as
'draft' and never updated — transitions write `current_state` only (workspace.py
``SET current_state``). The badge therefore showed Draft for a deal in any state.
`get_deals_list` did not select `current_state` at all, so no client of the list
endpoint could have recovered the real state even had it wanted to.

One line: ``current_state`` joins the CTE's column list, and the outer
``to_jsonb(d)`` picks it up automatically, so no JSON assembly changes. The
workspace path already returns it (0028) — this brings the list to parity.

``status`` and ``stage`` stay where they are for now: they are the duplicates this
migration stops reading, and dropping columns is a separate change with its own
blast radius.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "rn_009_deal_state_single_source"
down_revision: str | None = "rn_008_deal_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The CTE's column list in get_deals_list (0030). Single occurrence in the
# definition; rn_008's filter lives on the FROM line, not here.
_FIND = "facility_amount, status, document_count, document_required,"
_TO = "facility_amount, status, current_state, document_count, document_required,"


def _patch(find: str, to: str) -> None:
    op.execute(
        f"""
        DO $$
        DECLARE definition TEXT;
        BEGIN
            SELECT pg_get_functiondef('public.get_deals_list(uuid)'::regprocedure) INTO definition;
            IF position('{find}' IN definition) = 0 THEN
                RAISE EXCEPTION 'current_state anchor missing from get_deals_list: %',
                    left('{find}', 70);
            END IF;
            definition := replace(definition, '{find}', '{to}');
            EXECUTE definition;
        END $$;
        """
    )


def upgrade() -> None:
    _patch(_FIND, _TO)


def downgrade() -> None:
    _patch(_TO, _FIND)
