"""Import existing snapshots at today's knowledge time; never invent historical dates."""

from __future__ import annotations

import argparse
import json
from uuid import UUID

from creditpaper.db import close_pool, cursor
from creditpaper.ledger.service import commit_outputs


def main() -> None:
    parser = argparse.ArgumentParser()
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--tenant", type=UUID)
    scope.add_argument("--all", action="store_true", help="Operator migration across this database")
    args = parser.parse_args()
    imported = held = 0
    after = ""
    try:
        while True:
            with cursor() as cur:
                cur.execute(
                    """SELECT d.tenant_id,d.id FROM deals d JOIN deal_model_snapshots s
                    ON s.deal_id=d.id AND s.tenant_id=d.tenant_id WHERE d.ledger_revision=0 AND d.id>%s
                    AND (%s::uuid IS NULL OR d.tenant_id=%s) ORDER BY d.id LIMIT 100""",
                    (after, args.tenant, args.tenant),
                )
                rows = cur.fetchall()
            if not rows:
                break
            for tenant_id, deal_id in rows:
                try:
                    # Bootstrap reads and validates the current snapshot under the same deal lock.
                    commit_outputs(str(tenant_id), deal_id, [], legacy=True)
                    imported += 1
                except (ValueError, TypeError):
                    held += 1
                after = deal_id
        print(json.dumps({"imported": imported, "requires_reextraction": held}))
        if held:
            raise SystemExit(1)
    finally:
        close_pool()


if __name__ == "__main__":
    main()
