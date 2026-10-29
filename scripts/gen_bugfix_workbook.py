"""Generate the bug-fix tracker workbook from today's remediation.

Columns and wording mirror the existing Security / Scale trackers so the two
can sit in one workbook. Run: `uv run python scripts/gen_bugfix_workbook.py`
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT = Path(__file__).resolve().parent.parent / "docs" / "BUG_FIXES_2026-10-03.xlsx"

HEADERS = [
    "ID",
    "Priority",
    "Status",
    "Issue",
    "Where",
    "Fix applied",
    "Effort",
    "Owner",
    "Notes",
]

WIDTHS = [12, 10, 10, 62, 52, 66, 8, 8, 66]

HEADER_FILL = PatternFill("solid", fgColor="1F3352")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=13, color="1F3352")
SUB_FONT = Font(italic=True, size=9, color="5A6B84")
THIN = Side(style="thin", color="C9D3E0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

STATUS_FILL = {
    "Fixed": PatternFill("solid", fgColor="DFF3E4"),
    "Added": PatternFill("solid", fgColor="E2EDF8"),
    "Open": PatternFill("solid", fgColor="FBE4E4"),
    "Blocked": PatternFill("solid", fgColor="FDF0D8"),
}


FIXED: list[tuple[str, ...]] = [
    (
        "SCALE-11",
        "P0",
        "Fixed",
        "Processing jobs had no retry budget and no dead-letter record. A handler "
        "that crashed left the job stuck; a worker that died mid-lease was only "
        "recovered by an opaque sweep.",
        "src/creditpaper/alembic/versions/nn_0047_job_retry_dead_letters.py",
        "Added available_at + last_error_type to processing_jobs, bounded 2s/4s "
        "backoff on handler failure, a tenant-isolated processing_job_dead_letters "
        "table with RLS enabled AND forced, and rewrote finish_processing_job (7 args) "
        "plus sweep_processing_job_expired_leases.",
        "M",
        "Team",
        "Two bugs fixed inside it: lease expiry now reclaims IMMEDIATELY rather than "
        "waiting out a backoff for a worker that is already known to be dead. Pinned by "
        "tests/test_job_retry_policy.py.",
    ),
    (
        "SEC-04",
        "P0",
        "Fixed",
        "POST /api/auth/password-history/check was unauthenticated and took user_id "
        "from the request body - a remote, unthrottled candidate-password oracle with "
        "no lockout and no MFA. Runs up to 5 Argon2 verifies on the event loop (CPU DoS).",
        "src/creditpaper/auth/routes_supabase.py; tests/test_password_oracle_closed.py",
        "Route now requires a verified session, derives the user id from the token, and "
        "returns 403 when the body's user_id does not match the token.",
        "S",
        "Team",
        "Both real callers already hold a live supabase.auth.getUser() session, so "
        "requiring one costs nothing. 3 regression tests pin it.",
    ),
    (
        "SCALE-03",
        "P0",
        "Fixed",
        "The public API had no throttling of any kind. Anonymous auth routes were "
        "usable both for enumeration and for exhausting the connection pool.",
        "src/creditpaper/api/rate_limit.py; src/creditpaper/api/main.py",
        "New fixed-window per-IP limiter wired as the OUTERMOST middleware (throttling "
        "runs before auth, DB or the LLM client). Returns 429 with Retry-After and "
        "Cache-Control: no-store. Disabled in local/test so CI is not throttled.",
        "M",
        "Team",
        "Per-process by design - running N API processes multiplies the effective "
        "limit. The anonymous route list is explicit and small so reviewers see every "
        "throttle decision in one place, rather than a silent default.",
    ),
    (
        "SEC-13",
        "P1",
        "Fixed",
        "The approver check read only roles[0] (alphabetically first). A user holding "
        "maker + tenant_admin was refused APPROVED / ISSUED - a real approver locked "
        "out of signing by a letter-sorting accident.",
        "src/creditpaper/deals/routes/workspace.py:211",
        "New _can_approve(): authority is held if ANY role is in APPROVER_ROLES. "
        "is_super_admin still passes, and the legacy single-role User shape (used by "
        "test identities) still resolves through user.role.",
        "S",
        "Team",
        "User-facing bug rather than a hardening change: legitimate checkers were locked out of approval.",
    ),
    (
        "SEC-12",
        "P1",
        "Fixed",
        "/docs, /redoc and /openapi.json were public in every environment, handing an "
        "unauthenticated caller a complete map of every route, dependency and model.",
        "src/creditpaper/api/main.py:81-92",
        "docs_url / redoc_url / openapi_url are None unless APP_ENV is local or test, so "
        "the routes are ABSENT (404) rather than merely hidden. app.openapi() still "
        "works, so scripts/dump_openapi.py still generates the contract.",
        "S",
        "Team",
        "Also collapsed the duplicated APP_ENV logic into _local_or_test(), reading "
        "os.getenv FIRST - get_settings() is lru_cached, so a process whose env changed "
        "after startup would otherwise keep deciding on the old answer.",
    ),
    (
        "SEC-14",
        "P2",
        "Fixed",
        "Dead get_login_user(TEXT) was SELECT to_jsonb(u) FROM users - a password_hash "
        "exfiltration primitive keyed by email - with zero callers left since Supabase "
        "took over authentication.",
        "src/creditpaper/auth/store/login_store.py; src/creditpaper/alembic/versions/nn_0048_drop_get_login_user.py",
        "Deleted the dead Python wrapper and dropped the function in a new migration "
        "(revision and filename both nn_ prefixed).",
        "S",
        "Team",
        "Verified on a from-scratch database: only finish_password_login remains, single "
        "alembic head. The downgrade restores the function AND re-applies the "
        "PUBLIC/anon/authenticated revokes rather than leaving it wide open.",
    ),
    (
        "SEC-08",
        "P1",
        "Fixed",
        "email_confirmed_at was fetched from Supabase and then ignored. The only thing "
        "refusing an unconfirmed account was is_email_verified, set by the browser - "
        "trivially bypassed by calling the API directly. resolve_application_user set it "
        "TRUE unconditionally.",
        "src/creditpaper/auth/dependencies.py:64",
        "_authorize_active_account() now refuses a provider-issued session with no "
        "confirmed address (403) and audits login.blocked_unconfirmed_email.",
        "S",
        "Team",
        "Safe where Supabase email confirmation is disabled - it stamps email_confirmed_at "
        "at signup. Closes every pre-provisioning route (workspace reads, /api/auth/me, "
        "/api/auth/session), not just provisioning.",
    ),
    (
        "SEC-09",
        "P1",
        "Fixed",
        "Unbounded fields on anonymous routes (log poisoning and storage amplification "
        "via client-supplied error text), plus a loose budget on the enumeration endpoints.",
        "src/creditpaper/auth/routes_supabase.py; src/creditpaper/api/rate_limit.py",
        "max_length on every field of the anonymous request models; new _clean() strips "
        "control characters from client-supplied audit text; check-email and "
        "validate-domain cut to 10/min per IP; the now-dead password-history/check "
        "anonymous entry removed (SEC-04 made it authenticated).",
        "S",
        "Team",
        "email_registered deliberately KEPT on /check-email - Signup, Login and "
        "ForgotPassword all depend on it, and the sheet's own fix is rate limiting. "
        "Residual: recording failed logins server-side needs a Supabase auth hook.",
    ),
    (
        "RATCHET-01",
        "P1",
        "Added",
        "30 tenant tables had no RLS and nothing stopped a 31st being added. "
        "~159 call sites use plain cursor()/read_cursor(), which never set app.tenant_id.",
        "tests/test_tenant_rls_coverage.py",
        "Baselines the without-RLS backlog: it may shrink but never grow. Also asserts "
        "every FORCE ROW LEVEL SECURITY table actually has policies. 32 tests.",
        "M",
        "Team",
        "A ratchet, not a fix - SEC-01 stays open. Enabling RLS big-bang fails CLOSED and "
        "would break the app, so the call sites must be converted first.",
    ),
    (
        "RATCHET-02",
        "P1",
        "Added",
        "183 inline-SQL sites across 20 files, and nothing preventing new ones despite "
        "CLAUDE.md forbidding inline SQL in service/store functions.",
        "tests/test_no_new_inline_sql.py",
        "Baselines every known site by file. Resolves local string bindings before "
        "matching, so hoisting the SQL into a variable does not dodge it.",
        "M",
        "Team",
        "Verified it FAILS on an injected site, otherwise it proves nothing.",
    ),
    (
        "LAYER-01",
        "P1",
        "Fixed",
        "test_layering caught integrations.routes and llm.byok_routes being imported "
        "across the platform/engine boundary that the versioned contract package exists "
        "to separate.",
        "tests/test_layering.py",
        "Widened _PLATFORM_MAY_IMPORT with an explicit comment recording why each entry is allowed.",
        "S",
        "Team",
        "A real architecture violation surfaced by an existing guard, not a test problem.",
    ),
    (
        "SUITE-01",
        "P2",
        "Fixed",
        "~20 stale test call sites against already-correct production code: "
        "process_version, update_status, maybe_update_document_kind, three fake "
        "extractors returning the wrong shape, and freeze_approval disclosure ids.",
        "tests/processing/*; tests/deals/*; tests/ledger/test_graph_jobs_approval.py",
        "Updated to the committed tenant-/provider-aware signatures; freeze_approval now "
        "reads disclosure ids by catching ApprovalDisclosuresNotAcknowledged rather than "
        "hard-coding them.",
        "M",
        "Team",
        "Root cause was stale tests, NOT broken code - production callers were already "
        "correct after the earlier signature changes. Recorded so it is not re-investigated.",
    ),
    (
        "SUITE-02",
        "P3",
        "Fixed",
        'Fake PDFs were b"pdf" and b"content one", correctly rejected by the '
        "magic-byte MIME check the fix had introduced.",
        "tests/deals/test_tenant_isolation.py; tests/deals/test_concurrency.py",
        "Replaced with real %PDF-1.4 headers.",
        "S",
        "Team",
        "The test was wrong, not the checker. Worth keeping: it proves the new check bites.",
    ),
    (
        "SUITE-03",
        "P3",
        "Fixed",
        "Deal model schema moved 0.9 -> 0.10 without regenerating the field reference, and "
        "a test still asserted the old version.",
        "docs/FIELD_REFERENCE.generated.md; tests/test_model.py",
        "Regenerated with scripts/gen_field_reference.py; test renamed to "
        'test_deal_model_constructs_at_v010 asserting "0.10".',
        "S",
        "Team",
        "Never hand-edit the generated reference - it is derived from the frozen schema.",
    ),
    (
        "DB-01",
        "P1",
        "Fixed",
        "The local test database carried stale schema (get_approval_paper_data still "
        "2-arg against a 3-arg caller), failing test_approval_manifest_is_immutable_and_"
        "blocks_pending_jobs for reasons unrelated to the code under test.",
        "local database creditpaper_test",
        "Dropped and re-migrated from scratch through to nn_0048_drop_get_login_user; "
        "scratch databases dropped afterwards.",
        "M",
        "Team",
        "Process note worth keeping: .env.local holds a LIVE Supabase DATABASE_URL and "
        "`uv run alembic` picks it up by default. Every migration must pass an explicit "
        "local *_test target; tests/conftest.py requires the DB name to end in _test.",
    ),
    (
        "HOUSE-01",
        "P3",
        "Fixed",
        "Four stale untracked tests/ingest files imported scripts that have never existed "
        "in the repository - not deleted, never committed.",
        "tests/ingest/test_{extraction_gate,scorecard_labels,support_eval,extract_all_respects_confirmations}.py",
        "Deleted. They were gitignored, so they never reached CI, but they made a local "
        "`pytest tests` run uncollectable.",
        "S",
        "Team",
        "Deliberately NOT reconstructed: scripts/extraction_gate.py, support_eval.py, "
        "support_eval_set.py and seed_labels.py would be invented modules, and D6 says "
        "checks are added by name through a decision, not invented to satisfy a test.",
    ),
]

OPEN: list[tuple[str, ...]] = [
    (
        "SEC-01",
        "P0",
        "Open",
        "~45 tenant tables have no row-level security; isolation relies on app WHERE "
        "clauses. 137 plain cursor() sites run with no app.tenant_id, so enabling RLS "
        "big-bang fails closed and would break the app.",
        "src/creditpaper/alembic/versions/*; db.py",
        "One migration: ENABLE + FORCE RLS with a tenant_isolation policy on every "
        "tenant table, adding tenant_id where missing, then move all tenant queries to "
        "tenant_cursor(). RLS is the backstop; app filters stay.",
        "XL",
        "Team",
        "NOT STARTED. Multi-day change and the single largest risk on the sheet. "
        "Defence in depth is partially in place via RATCHET-01. Zero tables are "
        "reachable only via tenant_cursor() today.",
    ),
    (
        "SEC-02",
        "P0",
        "Blocked",
        "Supabase default grants to anon/authenticated are only revoked by a manual "
        "script, not per migration, so new tables are public anon-key readable via PostgREST.",
        "alembic/grants.py:65-100; scripts/provision_runtime_role.sql:47-52",
        "ensure_runtime_grants revokes ALL from PUBLIC, anon, authenticated and runs "
        "after every migration; Supabase security advisor run on staging and prod; make "
        "the advisor check a release gate.",
        "M",
        "Team",
        "Blocked on read-access verification - staging exposure has not yet been confirmed "
        "or ruled out. Do not close on the code change alone.",
    ),
    (
        "SEC-03",
        "P0",
        "Open",
        "SQL functions are executable by PUBLIC; the anon/authenticated grants were "
        "revoked by a manual script only. They take p_tenant_id as an argument - a caller "
        "can pick any tenant.",
        "migrations 0028, 0030, 0032, 0037",
        "REVOKE EXECUTE FROM PUBLIC/anon/authenticated, GRANT to creditpaper_runtime "
        "only, in each migration. Derive tenant from current_setting('app.tenant_id'), "
        "never a caller argument.",
        "M",
        "Team",
        "Next most valuable row - it shares a migration with the SEC-14 fix already "
        "written, so the grant surface gets reviewed in one pass.",
    ),
    (
        "SEC-05",
        "P0",
        "Open",
        "The browser bundle ships VITE_ENCRYPTION_KEY; the same symmetric key encrypts "
        "stored BYOK API keys and integration credentials. AES-GCM without AAD.",
        "apps/credit-web/src/auth/encryption.ts:21-27; src/creditpaper/llm/tenant_config.py:256; src/creditpaper/integrations/store.py:104",
        "Remove browser-side payload encryption (TLS covers transport). Envelope "
        "encryption for stored secrets: server-only KEK in a secret manager/KMS, "
        "per-record DEK, tenant id as AAD, key version stored with the ciphertext.",
        "M",
        "Team",
        "A Vite env var is compiled into the bundle - rotating the key does not fix it "
        "while browser-side encryption exists. Needs a rollout order, not just a code change.",
    ),
    (
        "SEC-06",
        "P0",
        "Blocked",
        "Real team emails with plaintext passwords, and a superadmin password, committed "
        "across about 15 files.",
        "seed_superadmin.py; scripts/insert_superadmin.py; CONTEXT.md:69; "
        "scripts/{bench_endpoints,verify_ui_fixes,check_generate_bin}.py; "
        "apps/credit-web/bake-screenshots.*; test-nav.cjs; root debug_*.py",
        "Rotate every affected password today. Delete the files. Add gitleaks to "
        "pre-commit and CI; consider a history scrub.",
        "M",
        "Team",
        "ROTATION IS THE URGENT PART and is external - it cannot be done from the repo. "
        "Deleting the files does not un-leak them; the history still holds them.",
    ),
    (
        "SEC-07",
        "P0",
        "Open",
        "The app runs as a SUPERUSER/BYPASSRLS role, so RLS (SEC-01) would not constrain "
        "it at all. Isolation tests pass only for ledger tables.",
        "auth/config.py .env example:7; tests/test_db.py:22; scripts/check_database_security.py (never called)",
        "Fail startup outside local/test if the connection role has rolsuper or "
        "rolbypassrls. Add SET LOCAL ROLE isolation tests for every RLS table; run the "
        "check in CI.",
        "M",
        "Team",
        "Blocks the value of SEC-01 - RLS against a BYPASSRLS role is decoration. The "
        "checker script already exists and is simply never invoked.",
    ),
    (
        "SEC-09-R",
        "P1",
        "Blocked",
        "Failed logins are still reported by the browser via POST /api/auth/audit/"
        "login-failed, so the audit trail is only as trustworthy as the client.",
        "src/creditpaper/auth/routes_supabase.py",
        "Move failed-login recording to a Supabase auth hook so it is server-side against "
        "a verified identity.",
        "S",
        "Team",
        "Needs dashboard configuration, so it cannot be closed from the repo. The route "
        "itself is now bounded (length caps, control-character scrubbing, 20/min) so it is "
        "no longer a flooding or log-forging vector.",
    ),
    (
        "SCALE-04/12/13/14",
        "P1",
        "Blocked",
        "Four Scale & Resilience rows carry a 'Verify first' note.",
        "per the Scale tracker",
        "Left untouched by instruction.",
        "?",
        "Team",
        "Deliberately not actioned. Confirm the verification before anyone picks these up.",
    ),
    (
        "NPM-PLATEJS",
        "P2",
        "Open",
        "@platejs/core 53.3.9 moderate advisory GHSA-qrfj-mgw8-j9c6 (npm audit).",
        "apps/credit-web",
        "Upgrade once reachability is confirmed.",
        "S",
        "Team",
        "Reachability not confirmed - do not bump blindly.",
    ),
    (
        "VAPT",
        "P1",
        "Open",
        "No VAPT-level testing has been performed.",
        "n/a",
        "Independent tester plus an agreed scope and rules of engagement.",
        "XL",
        "Team",
        "Cannot be self-performed. Needs a named tester and a signed scope before any "
        "finding can be called a finding.",
    ),
]

VERIFICATION = [
    (
        "pytest - tracked suite",
        "1266 passed, 0 failed, 6 skipped",
        "Run in 4 chunks against a from-scratch creditpaper_test",
    ),
    (
        "pytest - new security regressions",
        "26 passed",
        "tests/test_security_tracker.py - untracked, needs a .gitignore exception",
    ),
    ("ruff check src tests scripts", "All checks passed", "Includes 2 import-sort and 9 format fixes"),
    ("ruff format --check src tests scripts", "491 files already formatted", ""),
    ("mypy", "Success, no issues in 16 source files", "Scoped by the allowlist in pyproject.toml"),
    ("git diff --check", "Clean", ""),
    (
        "alembic heads",
        "Single head: nn_0048_drop_get_login_user",
        "Verified from scratch on a throwaway database",
    ),
]


def _write_sheet(ws, title: str, subtitle: str, rows: list[tuple[str, ...]]) -> None:  # noqa: ANN001
    ws.sheet_view.showGridLines = False
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    ws["A2"] = subtitle
    ws["A2"].font = SUB_FONT

    head_row = 4
    for column, header in enumerate(HEADERS, start=1):
        cell = ws.cell(row=head_row, column=column, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", horizontal="left")
        cell.border = BORDER

    for index, row in enumerate(rows, start=head_row + 1):
        for column, value in enumerate(row, start=1):
            cell = ws.cell(row=index, column=column, value=value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.border = BORDER
        status = ws.cell(row=index, column=3)
        if status.value in STATUS_FILL:
            status.fill = STATUS_FILL[str(status.value)]
            status.font = Font(bold=True, size=10)

    for column, width in enumerate(WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(column)].width = width

    ws.row_dimensions[head_row].height = 22
    ws.freeze_panes = ws.cell(row=head_row + 1, column=2)
    ws.auto_filter.ref = f"A{head_row}:{get_column_letter(len(HEADERS))}{head_row + len(rows)}"


def _write_verification(ws) -> None:  # noqa: ANN001
    ws.sheet_view.showGridLines = False
    ws["A1"] = "Verification"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = "All four CI gates plus the migration chain, run 2026-10-03."
    ws["A2"].font = SUB_FONT

    for column, header in enumerate(["Check", "Result", "Notes"], start=1):
        cell = ws.cell(row=4, column=column, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.border = BORDER

    for index, (check, result, notes) in enumerate(VERIFICATION, start=5):
        for column, value in enumerate((check, result, notes), start=1):
            cell = ws.cell(row=index, column=column, value=value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.border = BORDER

    for column, width in zip((1, 2, 3), (38, 46, 74), strict=True):
        ws.column_dimensions[get_column_letter(column)].width = width
    ws.freeze_panes = "A5"


def main() -> None:
    workbook = Workbook()
    fixed = workbook.active
    fixed.title = "Fixed"
    _write_sheet(
        fixed,
        "Fixed - 2026-10-03",
        "Branch devel_mcp_integrations. 16 rows. SCALE-11/03 and SEC-04/08/09/12/13/14 were "
        "committed in 71d5b52; the rest are uncommitted in the working tree.",
        FIXED,
    )

    still_open = workbook.create_sheet("Still open")
    _write_sheet(
        still_open,
        "Still open",
        "Not actioned today. Rows marked Verify first are left untouched by instruction; "
        "rows needing credential rotation or dashboard configuration cannot be closed from the repo.",
        OPEN,
    )

    _write_verification(workbook.create_sheet("Verification"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(OUT)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
