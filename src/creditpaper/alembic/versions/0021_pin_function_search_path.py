"""pin search_path on the ledger trigger functions.

Revision ID: 0021_pin_function_search_path
Revises: 0020_deal_activity_index

The ledger trigger functions from 0012 and 0013 resolve table names through the caller's
search_path. A role that can put its own schema ahead of `public` could make them resolve
to other objects. Pinning search_path closes that (Supabase security advisor lint 0011).

No behaviour change: the functions only reference objects in `public` and `pg_catalog`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021_pin_function_search_path"
down_revision: str | None = "0020_deal_activity_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTIONS = ("ledger_immutable()", "ledger_resolution_guard()", "ledger_supersession_guard()")


def upgrade() -> None:
    for fn in _FUNCTIONS:
        op.execute(f"ALTER FUNCTION public.{fn} SET search_path = pg_catalog, public")


def downgrade() -> None:
    for fn in _FUNCTIONS:
        op.execute(f"ALTER FUNCTION public.{fn} RESET search_path")
