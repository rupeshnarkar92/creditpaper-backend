# deals

Deal lifecycle: ingest, render, reviewer signals.

## What it does
- Lists deal IDs
- Serves section summaries and section detail for the in-app review surface
- Captures reviewer comments and edits as Signals
- Renders the final .docx via render_reviewed_paper

## Routes
| Method | Path | Purpose |
|--------|------|---------|
| GET    | /deals | List all deal IDs |
| GET    | /deals/{id}/sections | List sections for a deal |
| GET    | /deals/{id}/sections/{n} | Get section detail |
| POST   | /deals/{id}/sections/{n}/signals | Append reviewer signal |
| GET    | /deals/{id}/sections/{n}/signals | Read signals |
| POST   | /deals/{id}/render | Render .docx |

## DB tables owned
- `deals` — deal metadata
- `paper_prose` — cached generated prose
- `deal_model_snapshots` — historical model snapshots
- `render_results` — render history

## Owner
Engine team, #engine channel.
