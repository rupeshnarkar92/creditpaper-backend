# llm

The LLM choke point. This is the **one and only** place in the codebase that calls the Anthropic SDK.

NVIDIA's OpenAI-compatible hosted API also runs through this choke point.
See [NVIDIA configuration and timing](../../../docs/NVIDIA_INFERENCE.md) for
model environment variables, local PDF/OCR processing, profiling and local fallback.

## What it does
- `client.py` — `messages_create()` — the only SDK call site
- `providers.py` — provider abstraction
- `ollama.py` — local LLM alternative
- `pdf_render.py` — PDF rendering helpers for LLM input

## Rules
- **Every LLM call routes through `llm/client.py::messages_create`**.
- Truncation guard, streaming threshold, and cost ledger all live here.
- A structural test (`tests/test_llm_choke_point.py`) fails CI if any other file imports the Anthropic SDK directly.
- That test exists because a bypass was found in production code that had been silently truncating house-style playbooks.

## Owner
Platform team, #platform channel.
