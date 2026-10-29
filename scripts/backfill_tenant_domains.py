"""Backfill tenants.email_domain from the best evidence available.

Sources, best first:
  1. auth_audit_events.event_metadata->>'email' for the tenant (signup events
     store the creating user's address).
  2. invitations.email where the invitation belongs to the tenant's users
     (legacy invitations carry domain_id, not tenant_id — joined via users).
  3. The legacy `domains` table row matching a member user's domain_id.

Tenants with no evidence are reported and left NULL — the script never
fabricates a domain. Idempotent: re-running skips already-mapped tenants and
skips conflicts rather than dying on them.

Run:  uv run python scripts/backfill_tenant_domains.py [--apply]
Default is a dry run; --apply writes.
"""

from __future__ import annotations

import argparse
import sys

from creditpaper.auth.domain_validator import EMAIL_REGEX
from creditpaper.auth.tenant_domain import insert_tenant_with_domain, normalize_domain
from creditpaper.db import cursor

# Domains that cannot identify a firm even when found in evidence.
BLOCKED = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "icloud.com",
    "me.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "qq.com",
    "163.com",
    "gmx.com",
    "yandex.com",
    "mailinator.com",
    "guerrillamail.com",
}


def _evidence_domains() -> dict[str, str]:
    """tenant_id → best candidate domain, from all three sources."""
    candidates: dict[str, list[str]] = {}

    with cursor() as cur:
        cur.execute(
            """
            SELECT tenant_id, event_metadata->>'email'
            FROM auth_audit_events
            WHERE tenant_id IS NOT NULL AND event_metadata->>'email' IS NOT NULL
            """
        )
        for tenant_id, email in cur.fetchall():
            if email and EMAIL_REGEX.match(email):
                candidates.setdefault(str(tenant_id), []).append(email.split("@")[1].lower())

        cur.execute(
            """
            SELECT ur.tenant_id, i.email
            FROM invitations i
            JOIN users u ON u.id::text = i.email OR lower(u.email) = lower(i.email)
            JOIN user_roles ur ON ur.user_id = u.id
            WHERE ur.tenant_id IS NOT NULL AND i.email IS NOT NULL
            """
        )
        for tenant_id, email in cur.fetchall():
            if email and EMAIL_REGEX.match(email):
                candidates.setdefault(str(tenant_id), []).append(email.split("@")[1].lower())

        cur.execute(
            """
            SELECT ur.tenant_id, d.domain
            FROM user_roles ur
            JOIN users u ON u.id = ur.user_id
            JOIN domains d ON d.id = u.domain_id
            WHERE ur.tenant_id IS NOT NULL AND d.domain IS NOT NULL
            """
        )
        for tenant_id, domain in cur.fetchall():
            if domain:
                candidates.setdefault(str(tenant_id), []).append(domain.lower())

    best: dict[str, str] = {}
    for tenant_id, domains in candidates.items():
        # Majority vote, then alphabetical — deterministic across runs.
        counts: dict[str, int] = {}
        for d in domains:
            counts[d] = counts.get(d, 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if top:
            best[tenant_id] = top[0][0]
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = parser.parse_args()

    with cursor() as cur:
        cur.execute("SELECT id, name, email_domain FROM tenants ORDER BY created_at")
        tenants = cur.fetchall()

    evidence = _evidence_domains()
    mapped = skipped_taken = blocked = unmapped = 0

    for tenant_id, name, current in tenants:
        tid = str(tenant_id)
        if current:
            print(f"  skip  {name} — already mapped to {current}")
            continue
        domain = evidence.get(tid)
        if not domain:
            print(f"  UNMAPPED  {name} ({tid}) — no evidence found; map manually")
            unmapped += 1
            continue
        if domain in BLOCKED:
            print(f"  UNMAPPED  {name} ({tid}) — evidence is a personal domain ({domain}); map manually")
            blocked += 1
            continue
        try:
            domain = normalize_domain(f"x@{domain}")
        except ValueError:
            print(f"  UNMAPPED  {name} ({tid}) — evidence is not a valid domain ({domain})")
            unmapped += 1
            continue

        if args.apply:
            if not insert_tenant_with_domain(tid, name, domain):
                print(f"  CONFLICT  {name} — {domain} already claimed by another tenant")
                skipped_taken += 1
                continue
        print(f"  {'applied' if args.apply else 'would apply'}  {name} → {domain}")
        mapped += 1

    print(
        f"\n{'applied' if args.apply else 'dry run'}: {mapped} mapped, "
        f"{unmapped + blocked} unmapped, {skipped_taken} conflicts"
    )
    if unmapped + blocked:
        print(
            "Map the unmapped tenants manually:\n"
            "  UPDATE tenants SET email_domain = '<domain>' WHERE id = '<tenant_id>';"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
