"""Republish due PostgreSQL jobs after Redis/Celery downtime.

Safe to run repeatedly: task ids are deterministic and PostgreSQL claim/fence
state prevents duplicate durable writes.
"""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--kinds")
    args = parser.parse_args()
    from creditpaper.queueing.config import migrated_kinds

    kinds = (
        [kind.strip() for kind in args.kinds.split(",") if kind.strip()]
        if args.kinds
        else migrated_kinds()
    )
    os.environ["CREDITPAPER_PROCESS_ROLE"] = "worker"

    from creditpaper.queueing import recovery

    result = recovery.recover_pending(per_tenant_limit=args.limit, kinds=kinds)
    print(" ".join(f"{key}={value}" for key, value in result.items()))


if __name__ == "__main__":
    main()
