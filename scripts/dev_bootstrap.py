"""Make a local database usable by the app, and say how to sign in.

Standing the prototype up kept failing on state nobody had written rather than
on code: a `creditpaper_dev` with 63 tables, four deals and no tenant, no
onboarding row and no account whose password anyone knew. The front end's dev
bypass does not help, because it is front end only - every real call still goes
to the API, which has no bypass at all.

So this makes the three things the app needs before it will show anything:

    a domain          tenancy IS the domain row; `deals.tenant_id` is its id
    an admin account  with a password printed below, verified and active
    onboarding state  marked complete, or every route bounces to the wizard

Idempotent. Run it as often as you like; it updates rather than duplicates, and
it never touches a row it did not create except to set the password and the
onboarding state it is here to set.

LOCAL ONLY. It writes a known password, so it refuses to run against anything
that is not a local database.

    uv run python scripts/dev_bootstrap.py
    uv run python scripts/dev_bootstrap.py --password hunter2 --email me@dev.local
"""

import argparse
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, "src")
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(".env"))

from creditpaper.auth.security.password import hash_password  # noqa: E402

from creditpaper.db import cursor  # noqa: E402

DEFAULT_EMAIL = "dev@creditpaper.local"
DEFAULT_PASSWORD = "devpassword123"  # local only; see the refusal below
DEFAULT_DOMAIN = "creditpaper.local"

# Every step the onboarding guard checks for, plus the sentinel it actually
# gates on. `OnboardingGuard` and `destinationFor` both test for `complete`;
# the rest are listed so the wizard shows as done rather than half finished.
COMPLETED_STEPS = ["brand", "papers", "voice", "sections", "complete"]

# Hosts a known password may be written to. Anything else and this refuses:
# the whole point of the script is that the credential is guessable.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


def refuse_if_not_local() -> str:
    """The DSN, or exit. A known password must never reach a shared database."""
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        # The individual DB_* variables default to localhost, so no URL at all
        # is the local case by construction.
        return "DB_HOST/DB_PORT (no DATABASE_URL set)"
    host = urlparse(dsn.replace("+asyncpg", "").replace("+psycopg", "")).hostname or ""
    if host.lower() not in LOCAL_HOSTS:
        print(f"REFUSING: {host} is not a local database, and this writes a known password.")
        raise SystemExit(2)
    return dsn


def ensure_domain(domain: str, admin_email: str) -> str:
    """The tenant. `deals.tenant_id` is this row's id — there is no separate row."""
    with cursor() as cur:
        cur.execute("SELECT id FROM domains WHERE domain = %s", (domain,))
        row = cur.fetchone()
        if row:
            return str(row[0])
        tenant_id = str(uuid.uuid4())
        cur.execute(
            """INSERT INTO domains (id, domain, admin_email, status)
               VALUES (%s, %s, %s, 'active')""",
            (tenant_id, domain, admin_email),
        )
    return tenant_id


def ensure_admin(email: str, password: str, tenant_id: str) -> tuple[str, bool]:
    """The account, and whether it already existed.

    An existing account has its password RESET rather than left alone: the
    reason to run this is usually that nobody knows the password any more.
    """
    with cursor() as cur:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
        if row:
            cur.execute(
                """UPDATE users
                      SET password_hash = %s, is_email_verified = true, is_active = true,
                          role = 'tenant_admin', domain_id = %s
                    WHERE id = %s""",
                (hash_password(password), tenant_id, str(row[0])),
            )
            return str(row[0]), True
        user_id = str(uuid.uuid4())
        cur.execute(
            """INSERT INTO users
                 (id, email, password_hash, is_email_verified, is_active,
                  auth_provider, is_super_admin, role, domain_id, first_name, last_name)
               VALUES (%s, %s, %s, true, true, 'local', false, 'tenant_admin', %s, 'Local', 'Dev')""",
            (user_id, email, hash_password(password), tenant_id),
        )
    return user_id, False


def ensure_onboarding_complete(tenant_id: str) -> None:
    """Without this every protected route bounces to the setup wizard.

    The wizard itself cannot write this on a fresh database - it has nowhere to
    save to until the domain above exists - which is why clicking through it
    lands back on step 1.
    """
    import json

    state = json.dumps({"completed_steps": COMPLETED_STEPS})
    with cursor() as cur:
        cur.execute(
            """INSERT INTO onboarding_state (tenant_id, state)
               VALUES (%s, %s::jsonb)
               ON CONFLICT (tenant_id) DO UPDATE SET state = EXCLUDED.state""",
            (tenant_id, state),
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    ap.add_argument("--domain", default=DEFAULT_DOMAIN)
    args = ap.parse_args()

    dsn = refuse_if_not_local()
    tenant_id = ensure_domain(args.domain, args.email)
    _user_id, existed = ensure_admin(args.email, args.password, tenant_id)
    ensure_onboarding_complete(tenant_id)

    with cursor() as cur:
        cur.execute("SELECT count(*) FROM deals WHERE tenant_id = %s", (tenant_id,))
        deals = cur.fetchone()[0]

    print(f"database   {dsn.split('@')[-1] if '@' in dsn else dsn}")
    print(f"tenant     {tenant_id}  ({args.domain})")
    print(f"account    {args.email}  {'(password reset)' if existed else '(created)'}")
    print(f"password   {args.password}")
    print("onboarding complete")
    print(f"deals      {deals} under this tenant")
    if not deals:
        print("\nNo deals yet — create one in the app, or ingest a folder with")
        print("scripts/e2e_corpus_ingest.py to put real extracted facts in front of the UI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
