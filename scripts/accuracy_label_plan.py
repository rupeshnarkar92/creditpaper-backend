"""Report release-label coverage without emitting document text or fact values."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def concept_family(path: str) -> str:
    return re.split(r"[.\[]", path, maxsplit=1)[0] or "other"


def build_plan(
    labels: list[Any],
    *,
    min_fields: int = 73,
    min_deals: int = 3,
    min_families: int = 5,
    target_per_deal: int = 15,
    target_per_family: int = 8,
) -> dict:
    latest: dict[tuple[str, str], Any] = {}
    for label in labels:
        latest[(label.deal_id, label.concept_path)] = label
    deals = Counter(label.deal_id for label in latest.values())
    families = Counter(concept_family(label.concept_path) for label in latest.values())
    held = [label for label in latest.values() if not getattr(label, "passed", False)]
    classified_holds = sum(bool(getattr(label, "verify_reasons", ())) for label in held)
    reason_coverage = round(classified_holds / len(held), 3) if held else 1.0
    return {
        "labels": len(latest),
        "deals": len(deals),
        "concept_families": len(families),
        "labels_by_deal": dict(sorted(deals.items())),
        "labels_by_family": dict(sorted(families.items())),
        "review_reason_coverage": {
            "held_labels": len(held),
            "classified": classified_holds,
            "rate": reason_coverage,
        },
        "collection_priorities": {
            "additional_deals_needed": max(0, min_deals - len(deals)),
            "additional_families_needed": max(0, min_families - len(families)),
            "deal_label_shortfalls": {
                deal: target_per_deal - count
                for deal, count in sorted(deals.items())
                if count < target_per_deal
            },
            "family_label_shortfalls": {
                family: target_per_family - count
                for family, count in sorted(families.items())
                if count < target_per_family
            },
        },
        "deficits": {
            "labels": max(0, min_fields - len(latest)),
            "deals": max(0, min_deals - len(deals)),
            "concept_families": max(0, min_families - len(families)),
        },
        "ready_for_scoring": len(latest) >= min_fields
        and len(deals) >= min_deals
        and len(families) >= min_families,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    from creditpaper.db import tenant_uuid
    from creditpaper.ledger.labels import labels_for

    try:
        tenant_id = str(tenant_uuid(args.tenant))
    except ValueError:
        parser.error("--tenant must be a uuid")
    try:
        labels = labels_for(tenant_id, latest_only=True)
    except Exception as exc:  # CLI boundary: report infrastructure failure without a traceback.
        parser.exit(3, f"could not read tenant labels: {exc}\n")
    plan = build_plan(labels)
    rendered = json.dumps(plan, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if plan["ready_for_scoring"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
