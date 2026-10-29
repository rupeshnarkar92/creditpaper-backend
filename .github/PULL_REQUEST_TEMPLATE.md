## What changed, and why

<!-- The why matters more than the what — the diff already says what. If this
     fixes a defect, say what the defect would have produced in a credit paper. -->

## Verification

<!-- Evidence, not assertion. Paste output rather than claiming a pass. -->

- [ ] `pytest`
- [ ] `ruff check src tests scripts`
- [ ] `ruff format --check src tests scripts`
- [ ] `mypy`
- [ ] For a fix: a regression test that I **confirmed fails** against the unfixed code

CI also runs the web build, both Python versions, the generated-file drift gates
and a secret scan. A green local run is necessary, not sufficient.

## Non-negotiables (D1–D12, `docs/BUILD_PLAN.md` §1)

Tick only what this PR touches. **A change that conflicts with one of these is
escalated as a `DECISIONS.md` entry, never resolved silently in code.**

- [ ] **D4 — no anchor, no fact.** Every new fact carries provenance; prose cites
      only model fields that exist.
- [ ] **D8 — the Section 9 recommendation stays human-authored.**
- [ ] **Write path** — model changes go through `model/apply.py`; extractors emit
      `ExtractionOutput` and never touch the model.
- [ ] **LLM choke point** — any model call goes through `llm/client.py`.
- [ ] **Schema change** — recorded in `DECISIONS.md`, then
      `scripts/gen_field_reference.py` re-run and the result committed.
- [ ] **Contract change** (`src/creditpaper/contracts/`) — this is the published
      seam. Version bumped, and the platform side coordinated; `dump_openapi.py`
      re-run and `api-types.ts` committed.
- [ ] **Tenant data** — no deal files, signed papers, prior papers, client
      identity or real figures in the diff, including in test fixtures, prompts
      and commit messages.

## Anything you are unsure about

<!-- Say so here. An unstated assumption that turns out wrong is more expensive
     than a question. -->
