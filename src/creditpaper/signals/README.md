# signals

Reviewer signals — comments, edits, anchored text changes.

## What it does
- Captures reviewer activity on AI-generated sections
- Stores comments, before/after text, anchored text spans
- Persists the latest edit text for a (deal, section) pair
- Read-only history (append-only)

## Storage
- DB: `signals` table (id, deal_id, section, kind, author, comment_text, anchored_text, before_text, after_text, created_at)
- Disk: `output/signals/{tenant_id}/{deal_id}/{section}/` (dev-persisted JSON)

## Routes
Signals are served via the deals router (`/deals/{id}/sections/{n}/signals`).
The signal storage layer is in `signals/utils/store.py`.

## Owner
Engine team, #engine channel.
