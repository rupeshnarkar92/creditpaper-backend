# auth

Supabase-native authentication for CreditPaper. Supabase owns identity
(signup, login, passwords, email verification, sessions, JWTs); this module
verifies Supabase JWTs, resolves firm membership from local RBAC rows, and
provisions a firm's first tenant on signup completion.

## What it does

- JWT verification (ES256 JWK set, HS256 fallback) — `supabase.py`
- `CurrentUser` dependency + `require_role` / `require_permission` — `dependencies.py`
- Role/permission lookups against `roles` / `permissions` / `role_permissions` / `user_roles` — `rbac.py`
- First-run firm provisioning (tenant + `tenant_admin` role + membership) — `provisioning.py`
- Auth audit events — `auth_audit.py`
- Company-domain validation for signup — `domain_validator.py`
- Supabase Google Workspace/Azure tenant policy, active-account checks, and local profile linking — `dependencies.py`, `supabase_admin.py`

## Routes (`routes_supabase.py`, prefix `/api/auth`)

| Method | Path | Purpose |
|--------|------|---------|
| POST | /api/auth/validate-domain | Reject personal email domains at signup |
| POST | /api/auth/check-email | UX hint; Supabase enforces uniqueness |
| POST | /api/auth/complete-signup | Provision tenant + admin membership (idempotent) |
| POST | /api/auth/audit/signup-initiated | Record signup started (auth required) |
| POST | /api/auth/audit/login-success | Record successful login (auth required) |
| POST | /api/auth/audit/login-failed | Record failed login (public) |
| POST | /api/auth/logout | Record logout (auth required) |
| GET  | /api/auth/me | Current user profile + roles + permissions |

Login, signup, password reset, and email verification run through the Supabase client SDK,
but every authenticated API request re-validates the Supabase session, public-domain policy,
provider organization policy, active account state, local profile, tenant membership, and RBAC.
The frontend never stores OAuth tokens in localStorage; Supabase persists its session using its
managed client storage, while application authorization remains server-side.

Configure:

- `SUPABASE_MICROSOFT_TENANT_ID` — Azure Directory/Tenant ID for the single-tenant app.
Signup/account-auth database operations (domain/email status, atomic tenant provisioning,
Supabase identity context, local profile plus RBAC resolution, password history, and auth
audit insertion) are implemented in migration `0037_auth_signup_sp`. Functions are revoked
from browser roles; the two reads of Supabase's `auth` schema are narrowly scoped
`SECURITY DEFINER` functions. Apply Alembic with the migration-owner credentials before
deploying application code that calls these routines.

In Supabase, configure Google with the Workspace provider and Azure with a Single Tenant
Entra application. The API fails closed if the provider identity or required tenant claim
cannot be read from `auth.identities`.

## DB tables owned

- `tenants` — firms (created by `provisioning.py`)
- `roles` / `permissions` / `role_permissions` / `user_roles` — RBAC
- `auth_audit_events` — auth audit trail

## Dependencies on other domains

- `onboarding` — users complete onboarding before accessing the app

## Owner

Platform team, #auth channel.
