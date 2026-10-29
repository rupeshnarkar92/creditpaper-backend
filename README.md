# creditpaper

Turns a lender's pile of deal documents into a credit paper that a credit committee can
scrutinise — every figure traceable to the clause it came from.

Australian CRE private credit (first mortgage, wholesale-funded). Working name; neutral until
the brand is settled.

```
documents → route → extract → deal model → checks → analysis → paper → human review
                                   ▲                                        │
                                   └──────────── learning ──────────────────┘
```

**The product is the typed deal model, not the document.** Papers are rendered from the model;
no agent ever writes into the .docx. Renderers are disposable, the model is not.

## Status, honestly

The engine runs end to end on real deals and is under active reshaping. It has **not** passed
the blind validation gate (D9/D11) and nothing here is client-facing yet. The check registry is
names-only, the learning loop captures signal but nothing mines it yet, and the API has no
authentication. `docs/SCOPE_VS_REALITY.md` states scope against what exists, and
`docs/HANDOFF.md` lists what is known broken.

## What makes it different

**Every fact carries its provenance.** A `Fact` cannot be constructed without an anchor — no
anchor, no fact, enforced in the constructor rather than checked afterwards. Every figure in a
generated paper resolves to the document, page and clause it came from, rendered as a
provenance appendix keyed by source. (Per-figure click-through is the planned surface and is
not built.) For a lender facing an AFSL audit or a credit committee, that is the whole
proposition: one hallucinated figure ends the conversation.

**It catches things.** Cross-document reconciliation ties the borrower's funding table to the
signed term sheet, the QS cost lines to the funding lines, and the valuation's currency to the
expected funding date. On the build deal it surfaced a contingency shortfall — the QS
recommends 10%, the loan funds 3.8% — automatically. (That logic lives in
`harness/ingest.py::_reconcile`; the named registry in `checks/` holds the seventeen check
names, and wiring the two together is outstanding work.)

**It knows what it does not know.** Missing data renders as best-practice structure and a named
action for a named resolver, never as a blank or a confident guess.

**The human decides.** The Section 9 recommendation is human-authored and the paper cannot
reach approved state without it. Divergences from a specialist's recommendation are shown and
argued, not silently smoothed over.

## Getting started

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv venv
uv pip install -e ".[dev]"
cp .env.example .env          # fill in OPENROUTER_API_KEY

pytest                        # no API calls, no fixtures, no network
```

Set `LLM_PROVIDER=openrouter` and `OPENROUTER_API_KEY` in `.env` (or `.env.local`
when running with `APP_ENV=local`). The `OPENROUTER_PROSE_MODEL`,
`OPENROUTER_EXTRACT_MODEL`, `OPENROUTER_REVIEW_MODEL`, and
`OPENROUTER_PROOFREAD_MODEL` settings take OpenRouter model IDs such as
`anthropic/claude-sonnet-5`. They default to the project's Claude tiers and are
independent of the `ANTHROPIC_*_MODEL` overrides used by Anthropic and Ollama.
Restart the backend and workers after editing the environment file.

OpenRouter uses its [Anthropic-compatible Messages API](https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message),
preserving document inputs and response guards. `ANTHROPIC_TIMEOUT_S`,
`ANTHROPIC_MAX_RETRIES`, `ANTHROPIC_MAX_CONCURRENCY`, and `ANTHROPIC_EFFORT*`
also apply to this SDK path; `ANTHROPIC_INFERENCE_GEO` applies only to direct
Anthropic requests. OpenRouter models are flagged as unpriced in the local cost
ledger; consult OpenRouter for actual charges. Direct Anthropic remains available
with `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`; Ollama uses
`LLM_PROVIDER=ollama` and its existing local settings.

Then read **[`docs/README.md`](docs/README.md)** — it says which document answers which
question. If you are new to the repo, the shortest useful path is:

1. `docs/ARCHITECTURE.md` — how it is built, and what is enforced structurally
2. `docs/SCOPE_VS_REALITY.md` — what was agreed against what exists
3. `docs/HANDOFF.md` — current state and what is known broken
4. `docs/BUILD_PLAN.md` §1 — the twelve non-negotiables (D1–D12)
5. `docs/specs/2026-08-21-engine-architecture-design.md` — where the engine is going

## Running the pipeline

Live runs call the configured LLM provider; hosted providers cost real money — a full construction deal measured
**~$6.01 and ~16.5 minutes**. The cost ledger prints dollars and wall-clock for every run.
Deal fixtures are tenant data and are not in this repository.

```bash
scripts/ingest_deal.py --deal riverton                # documents → model, no prose
scripts/generate_paper.py --deal riverton             # full documents → .docx
scripts/validate_track1.py --deal riverton --mode ingest
scripts/render_from_ground_truth.py                # free: no API calls
```

The API and review app:

```bash
uvicorn creditpaper.api.main:app --reload          # engine API
cd apps/credit-web && npm install && npm run dev   # review surface
```

## Layout

| Path | Holds |
|---|---|
| `src/creditpaper/model/` | The deal model. Every fact carries value, provenance, confidence and status (D4). Writes go through `apply.py` only. |
| `src/creditpaper/extractors/` | One extractor per document type. Two-pass for long documents. Shared nothing but the schema convention. |
| `src/creditpaper/checks/` | The named, finite cross-document check registry (D6). |
| `src/creditpaper/agents/` | Thesis, section drafting, fidelity review, proofreading, and the constitution that governs all of them. |
| `src/creditpaper/harness/` | Routing, ingestion, reconciliation, Track-1 validation, house-style learning, signal capture. |
| `src/creditpaper/renderer/` | python-docx renderer. Deliberately plain output, shaped for Canva import (D2). |
| `src/creditpaper/obs/` | The cost ledger and the single LLM choke point every call routes through. |
| `src/creditpaper/api/` | FastAPI application (`creditpaper.api.main:app`). |
| `apps/credit-web/` | React + Plate review surface. |
| `docs/` | All documentation. Start at `docs/README.md`. |
| `supabase/` | Schema, with tenancy and RLS from day one. |
| `fixtures/` | Real deal files. **Gitignored — tenant data, never committed.** |

## Contributing

See **[`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md)** for conventions, the checks that must
pass, and the rules that are not negotiable.

The short version: read `docs/BUILD_PLAN.md` §1 before non-trivial work. The twelve banked
decisions are design rules, not preferences — a change that conflicts with one gets escalated
in `docs/DECISIONS.md`, never resolved silently.

## Licence

Proprietary and confidential. See [`LICENSE`](LICENSE). Prompts, expert briefs and the credit
knowledge base are trade secrets; deal documents are lender-confidential and never leave the
local machine.
