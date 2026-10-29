-- Tenancy from day one (Build Plan §2): tenant_id on every table, RLS on.
-- Supabase is auth + persistence only — no calc or analytical logic here.
--
-- ---------------------------------------------------------------------------
-- STATUS: DESIGN INTENT, NOT A LIVE SCHEMA. No Supabase project is deployed and
-- NOTHING IN THIS REPOSITORY READS OR WRITES THESE TABLES — there is no client
-- library in `src/`; the only mentions are comments naming this as the swap
-- target. Persistence today is files under `output/` (`harness/prose_store.py`,
-- `harness/signal_store.py`). Do not assume a row here exists anywhere.
--
-- Known defects, tracked as D-5 in docs/plans/2026-08-21-audit-remediation.md.
-- Read them before applying this to a real project:
--
--   * The RLS policies below key on `auth.jwt() ->> 'tenant_id'`. NOTHING
--     PROVISIONS THAT CLAIM. No auth flow in this repository issues it.
--   * The only credential the code carries is SUPABASE_SERVICE_ROLE_KEY, which
--     BYPASSES RLS ENTIRELY. So the isolation below is, in practice, off.
--   * Child tables carry `tenant_id` and separately reference `deals(id)`, with
--     nothing tying the two together. A row can name tenant A while pointing at
--     a deal owned by tenant B. Composite foreign keys would close this.
--   * There is NO `signals` table, and signals are the learning substrate. They
--     are the thing most in need of durable storage and the thing this schema
--     does not model.
--   * `tenant_id` is `uuid` here; in code it is a plain string defaulting to
--     "dev" (`contracts/signal.py`, `model/deal.py`). These do not yet meet.
--   * Tables use `if not exists` and are re-runnable; THE POLICY BLOCK IS NOT —
--     a second run raises "policy already exists". There is no migrations
--     directory, so there is no ordered path from one version to the next.
--
-- The platform team owns turning this into a real, migrated schema.
-- ---------------------------------------------------------------------------

create table if not exists tenants (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    created_at timestamptz not null default now()
);

create table if not exists deals (
    id uuid primary key default gen_random_uuid(),
    tenant_id uuid not null references tenants(id),
    name text not null,
    deal_type text not null check (deal_type in ('construction', 'bridge', 'residual_stock')),
    created_at timestamptz not null default now()
);

create table if not exists deal_model_snapshots (
    id uuid primary key default gen_random_uuid(),
    tenant_id uuid not null references tenants(id),
    deal_id uuid not null references deals(id),
    model_version int not null,
    schema_version text not null,
    model jsonb not null,           -- the full typed deal model (D1)
    created_at timestamptz not null default now(),
    unique (deal_id, model_version)
);

create table if not exists documents (
    id uuid primary key default gen_random_uuid(),
    tenant_id uuid not null references tenants(id),
    deal_id uuid not null references deals(id),
    slot_key text not null,
    filename text not null,
    storage_path text not null,
    slot_state text not null default 'filled'
        check (slot_state in ('filled', 'processing', 'validated', 'flagged')),
    uploaded_at timestamptz not null default now()
);

create table if not exists check_findings (
    id uuid primary key default gen_random_uuid(),
    tenant_id uuid not null references tenants(id),
    deal_id uuid not null references deals(id),
    model_version int not null,
    check_key text not null,
    passed boolean not null,
    message text not null,
    details jsonb,
    created_at timestamptz not null default now()
);

-- AFSL audit trail (D8): section approvals, including the human-authored
-- recommendation event on section 9.
create table if not exists section_events (
    id uuid primary key default gen_random_uuid(),
    tenant_id uuid not null references tenants(id),
    deal_id uuid not null references deals(id),
    section int not null,
    event text not null
        check (event in ('drafted', 'regenerated', 'human_edited', 'recommendation_authored', 'approved')),
    actor text not null,            -- user id or agent name
    created_at timestamptz not null default now()
);

-- Commercial primitives (DECISIONS.md 2026-06-11): the billable unit is a
-- paper's FIRST render in approved state. M1 records usage only — no pricing,
-- no entitlements, no payment plumbing (M2). The unique index makes "first"
-- structural: re-renders of an approved paper cannot double-bill.
create table if not exists billable_events (
    id uuid primary key default gen_random_uuid(),
    tenant_id uuid not null references tenants(id),
    deal_id uuid not null references deals(id),
    event_type text not null check (event_type in ('paper_first_approved_render')),
    metadata jsonb,
    occurred_at timestamptz not null default now()
);
create unique index if not exists billable_once_per_deal
    on billable_events (deal_id, event_type);

-- Usage telemetry: per-run token spend so real cost-per-paper is known before
-- pricing is ever quoted. Written by the pipeline for every extractor/agent run.
create table if not exists agent_runs (
    id uuid primary key default gen_random_uuid(),
    tenant_id uuid not null references tenants(id),
    deal_id uuid not null references deals(id),
    runner text not null,           -- extractor or section-agent name
    model text,                     -- LLM model id, null for deterministic runs
    input_tokens int,
    output_tokens int,
    created_at timestamptz not null default now()
);

-- RLS: on for every table; tenant isolation via JWT claim.
alter table tenants enable row level security;
alter table deals enable row level security;
alter table deal_model_snapshots enable row level security;
alter table documents enable row level security;
alter table check_findings enable row level security;
alter table section_events enable row level security;
alter table billable_events enable row level security;
alter table agent_runs enable row level security;

do $$
declare t text;
begin
  foreach t in array array['deals', 'deal_model_snapshots', 'documents', 'check_findings',
                           'section_events', 'billable_events', 'agent_runs']
  loop
    execute format(
      'create policy tenant_isolation on %I for all
         using (tenant_id = (auth.jwt() ->> ''tenant_id'')::uuid)
         with check (tenant_id = (auth.jwt() ->> ''tenant_id'')::uuid)', t);
  end loop;
end $$;

-- Tenants: users may read their own tenant row only (writes are service-role).
create policy tenant_self_read on tenants
    for select using (id = (auth.jwt() ->> 'tenant_id')::uuid);
