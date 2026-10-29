"""Guardrails module.

Lets a firm set its own rules for how the AI works — wording, structure,
always-do steps, always-flag thresholds, and outside-appetite property types.
Platform safety (agents/constitution.py PROHIBITIONS) always applies on top
and can never be relaxed by firm rules.
"""

from creditpaper.guardrails.models.guardrail import GuardrailEvaluation, GuardrailRule

__all__ = ["GuardrailEvaluation", "GuardrailRule"]
