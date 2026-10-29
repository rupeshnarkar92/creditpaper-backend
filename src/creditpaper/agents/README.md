# agents

Domain agents that emit typed findings about a deal.

## What it does
- `thesis.py` — generates the deal thesis
- `section.py` — writes individual credit paper sections
- `proofread.py` — proofreads generated prose
- `review.py` — review logic
- `constitution.py` — house-style constitution
- `jsonio.py` — JSON IO helpers for agent I/O

## Rules
- All agents emit findings, not enthusiasm.
- Agents may only reference facts that exist as model fields (D4).
- Every LLM call routes through `llm/client.py::messages_create`.

## Owner
Engine team, #engine channel.
