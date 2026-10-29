# guardrails

Firm-level rules for how the AI works. Platform safety (`agents/constitution.py`
PROHIBITIONS) always applies and is read-only here; firm rules only ever add.

## Rule kinds (fixed parts, no free text)

- `wording` — `{say, never}`
- `structure` — `{section, position: before|after, anchor}`
- `always_do` — `{step, sections?}`
- `always_flag` — `{metric, operator: above|below|equals, threshold, unit}`
- `outside_appetite` — `{property_types[]}`

## Scope

`scope_type: global|module`, `scope_value: deals|templates|onboarding|papers|null`.
`list_active(tenant, module)` returns global + module-matching rules.

## Enforcement (future wiring)

`firm_rules_text(tenant_id, module)` renders active rules for prompt
injection after platform safety. `evaluate_rule` is a pure dispatcher used
by the render path; results go to `guardrail_evaluations` and feed the
accuracy rollup. Scores are informational and never gate approval.
