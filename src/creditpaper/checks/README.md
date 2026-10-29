# checks

Cross-document checks (D6). Each check is a named, finite function.

## What it does
- `registry.py` — the CHECKS dict: `{"name": logic_function, ...}`
- `base.py` — CheckContext, CheckResult types

## Rules (D6)
- New checks are added by name. No open-ended consistency engine.
- Every check has a clear name, inputs, and outputs.
- A failing check blocks the paper from advancing.

## Owner
Engine team, #engine channel.
