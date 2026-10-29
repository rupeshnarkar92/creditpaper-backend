"""The generation harness (spec docs/superpowers/specs/2026-09-30-generation-harness-design.md).

Engine-side: reads a frozen deal model, never documents. Every model call goes
through `creditpaper.llm.client.messages_create`.
"""
