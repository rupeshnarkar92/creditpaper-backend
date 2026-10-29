"""Guardrail service — validation, duplicates, evaluation, accuracy.

Kinds are fixed; every rule is composed from parts, never free text.
Firm rules only ever ADD to platform safety — enforcement always prepends
the platform constitution text first (see firm_rules_text).
"""

from __future__ import annotations

import re
from typing import Any

from creditpaper.guardrails.store import guardrail_store

KINDS = ("wording", "structure", "always_do", "always_flag", "outside_appetite")
SEVERITIES = ("info", "warn", "block")
MODULES = ("deals", "templates", "onboarding", "papers")
OPERATORS = ("above", "below", "equals")

# Short UI labels for the platform prohibitions in agents/constitution.py.
PLATFORM_LABELS = {
    "unsupported_claim": "no unsupported claims",
    "promotional": "no promotional language",
    "forward_looking": "no forward-looking certainty",
    "unattributed_opinion": "external opinions attributed",
    "no_machine_recommendation": "no machine recommendation",
    "adverse_characterisation": "no adverse characterisation",
}


def platform_rules() -> list[dict[str, str]]:
    """Read-only platform safety list for the UI card."""
    from creditpaper.agents.constitution import PROHIBITION_CATEGORIES

    return [
        {"key": key, "label": PLATFORM_LABELS.get(key, key.replace("_", " "))}
        for key in PROHIBITION_CATEGORIES
    ]


def firm_rules_text(tenant_id: str, module: str | None = None) -> str:
    """Render active firm rules as prompt text, appended AFTER platform safety.

    Called by generation paths; pass the calling module so module-scoped
    rules only fire where they belong. Returns "" when no rules apply.
    """
    rules = guardrail_store.list_active(tenant_id, module=module)
    if not rules:
        return ""
    lines = []
    for r in rules:
        lines.append(f"- [{r['kind']}] {describe(r)}")
    return "Firm rules (in addition to platform safety, never instead of it):\n" + "\n".join(lines)


def describe(rule: dict[str, Any]) -> str:
    """One-line human sentence for a rule — the same string the UI shows."""
    kind = rule["kind"]
    p = rule.get("payload") or {}
    if kind == "wording":
        return f"Say {p.get('say', '?')}, never {p.get('never', '?')}"
    if kind == "structure":
        return f"{p.get('section', '?')} {p.get('position', 'before')} {p.get('anchor', '?')}"
    if kind == "always_do":
        return str(p.get("step", "?"))
    if kind == "always_flag":
        return (
            f"Any {p.get('metric', '?')} {p.get('operator', 'above')} "
            f"{p.get('threshold', '?')}{p.get('unit', '')}"
        ).strip()
    if kind == "outside_appetite":
        types = ", ".join(p.get("property_types", [])) or "?"
        return f"Flag property types: {types}"
    return kind


def _validate_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize + validate fixed parts per kind. Raises ValueError."""
    p = dict(payload or {})
    if kind == "wording":
        say, never = str(p.get("say", "")).strip(), str(p.get("never", "")).strip()
        if not say or not never:
            raise ValueError("WORDING_NEEDS_SAY_AND_NEVER")
        if say.lower() == never.lower():
            raise ValueError("WORDING_TERMS_MUST_DIFFER")
        return {"say": say, "never": never}
    if kind == "structure":
        section, anchor = str(p.get("section", "")).strip(), str(p.get("anchor", "")).strip()
        position = str(p.get("position", "before")).strip().lower()
        if not section or not anchor:
            raise ValueError("STRUCTURE_NEEDS_SECTION_AND_ANCHOR")
        if position not in ("before", "after"):
            raise ValueError("STRUCTURE_BAD_POSITION")
        if section.lower() == anchor.lower():
            raise ValueError("STRUCTURE_SECTIONS_MUST_DIFFER")
        return {"section": section, "position": position, "anchor": anchor}
    if kind == "always_do":
        step = str(p.get("step", "")).strip()
        if not step:
            raise ValueError("ALWAYS_DO_NEEDS_STEP")
        out: dict[str, Any] = {"step": step}
        if p.get("sections"):
            out["sections"] = [str(s) for s in p["sections"]]
        return out
    if kind == "always_flag":
        metric = str(p.get("metric", "")).strip().lower()
        operator = str(p.get("operator", "")).strip().lower()
        if not metric:
            raise ValueError("FLAG_NEEDS_METRIC")
        if operator not in OPERATORS:
            raise ValueError("FLAG_BAD_OPERATOR")
        try:
            threshold = float(p.get("threshold"))
        except (TypeError, ValueError):
            raise ValueError("FLAG_BAD_THRESHOLD") from None
        return {
            "metric": metric,
            "operator": operator,
            "threshold": threshold,
            "unit": str(p.get("unit", "")),
        }
    if kind == "outside_appetite":
        types = [str(t).strip() for t in (p.get("property_types") or []) if str(t).strip()]
        if not types:
            raise ValueError("APPETITE_NEEDS_TYPES")
        return {"property_types": sorted(set(types))}
    raise ValueError("UNKNOWN_KIND")


def _normalize_for_dupe(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Canonical form used for duplicate comparison."""
    p = _validate_payload(kind, payload)
    if kind == "wording":
        return {"say": p["say"].lower(), "never": p["never"].lower()}
    if kind == "always_flag":
        return {
            "metric": p["metric"],
            "operator": p["operator"],
            "threshold": p["threshold"],
            "unit": p.get("unit", ""),
        }
    if kind == "outside_appetite":
        return {"property_types": p["property_types"]}
    if kind == "structure":
        return {k: p[k].lower() for k in ("section", "position", "anchor")}
    return {"step": p["step"].lower()}


def _find_duplicate(
    tenant_id: str, kind: str, payload: dict[str, Any], exclude_id: str | None = None
) -> dict[str, Any] | None:
    wanted = _normalize_for_dupe(kind, payload)
    for row in guardrail_store.list_all(tenant_id):
        if not row["is_active"] or row["kind"] != kind:
            continue
        if exclude_id and str(row["id"]) == str(exclude_id):
            continue
        if _normalize_for_dupe(row["kind"], row["payload"]) == wanted:
            return row
    return None


def _validate_scope(scope_type: str, scope_value: str | None) -> tuple[str, str | None]:
    scope_type = (scope_type or "global").strip().lower()
    if scope_type not in ("global", "module"):
        raise ValueError("SCOPE_BAD_TYPE")
    if scope_type == "module":
        if (scope_value or "").strip().lower() not in MODULES:
            raise ValueError("SCOPE_BAD_MODULE")
        return "module", scope_value.strip().lower()  # type: ignore[union-attr]
    return "global", None


# Top-level field names a direct API caller may send instead of nesting
# them under `payload`. Only these are merged; anything else is ignored.
FLAT_PAYLOAD_KEYS = frozenset(
    {
        "say",
        "never",
        "section",
        "position",
        "anchor",
        "step",
        "sections",
        "metric",
        "operator",
        "threshold",
        "unit",
        "property_types",
    }
)


def merge_flat_fields(payload: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    """Fold flat top-level rule fields into the payload dict.

    Lets direct API callers send `{"kind": ..., "say": ..., "never": ...}`
    instead of nesting under `payload`. Explicit `payload` keys always win;
    unknown extras are dropped. Returns a new dict.
    """
    merged = dict(payload or {})
    for key, value in (extra or {}).items():
        if key in FLAT_PAYLOAD_KEYS and key not in merged:
            merged[key] = value
    return merged


def list_rules(tenant_id: str) -> dict[str, Any]:
    return {
        "platform": platform_rules(),
        "rules": guardrail_store.list_all(tenant_id),
    }


def get_rule(tenant_id: str, rule_id: str) -> dict[str, Any] | None:
    return guardrail_store.get_by_id(tenant_id, rule_id)


def create_rule(
    tenant_id: str,
    *,
    kind: str,
    payload: dict[str, Any],
    severity: str = "info",
    weight: float = 1.0,
    scope_type: str = "global",
    scope_value: str | None = None,
) -> dict[str, Any]:
    """Create a firm rule.

    Raises:
        ValueError("DUPLICATE_GUARDRAIL"): same kind + normalized payload +
            same scope already exists for this tenant.
    """
    if kind not in KINDS:
        raise ValueError("UNKNOWN_KIND")
    if severity not in SEVERITIES:
        raise ValueError("BAD_SEVERITY")
    if not (0 < float(weight) <= 10):
        raise ValueError("BAD_WEIGHT")
    scope_type, scope_value = _validate_scope(scope_type, scope_value)
    clean = _validate_payload(kind, payload)
    if _find_duplicate(tenant_id, kind, clean) is not None:
        raise ValueError("DUPLICATE_GUARDRAIL")
    return guardrail_store.create(
        tenant_id,
        kind=kind,
        payload=clean,
        severity=severity,
        weight=float(weight),
        scope_type=scope_type,
        scope_value=scope_value,
    )


def update_rule(tenant_id: str, rule_id: str, **fields: Any) -> dict[str, Any] | None:
    """Partial update. Re-validates payload/kind/scope; enforces duplicates.

    Raises:
        ValueError("DUPLICATE_GUARDRAIL"): another rule already has the
            resulting kind + payload + scope.
    """
    existing = guardrail_store.get_by_id(tenant_id, rule_id)
    if not existing:
        return None
    if "severity" in fields and fields["severity"] is not None and fields["severity"] not in SEVERITIES:
        raise ValueError("BAD_SEVERITY")
    if "weight" in fields and fields["weight"] is not None and not (0 < float(fields["weight"]) <= 10):
        raise ValueError("BAD_WEIGHT")
    kind = existing["kind"]
    payload = fields.get("payload", existing["payload"])
    scope_type = fields.get("scope_type", existing["scope_type"])
    scope_value = fields.get("scope_value", existing["scope_value"])
    scope_type, scope_value = _validate_scope(scope_type, scope_value)
    clean = _validate_payload(kind, payload)
    if _find_duplicate(tenant_id, kind, clean, exclude_id=rule_id) is not None:
        raise ValueError("DUPLICATE_GUARDRAIL")
    fields = {**fields, "payload": clean, "scope_type": scope_type, "scope_value": scope_value}
    return guardrail_store.update(tenant_id, rule_id, **fields)


def delete_rule(tenant_id: str, rule_id: str) -> bool:
    """Deactivate (audit row kept for papers that referenced it)."""
    return guardrail_store.deactivate(tenant_id, rule_id)


# ---------------------------------------------------------------------------
# Deterministic evaluators — pure functions over explicit inputs.
# ---------------------------------------------------------------------------


def evaluate_rule(
    rule: dict[str, Any],
    *,
    text: str = "",
    order: list[str] | None = None,
    facts: dict[str, Any] | None = None,
    property_type: str | None = None,
) -> dict[str, Any]:
    """Evaluate one rule. Returns {"result": pass|flagged, "measured": {...}}.

    Reads only what is passed in — citable facts and rendered prose — so the
    same call works in tests and in the render path.
    """
    kind = rule["kind"]
    p = rule.get("payload") or {}
    if kind == "wording":
        hits = len(re.findall(r"\b" + re.escape(str(p.get("never", ""))) + r"\b", text, flags=re.I))
        if hits:
            return {"result": "flagged", "measured": {"hits": hits, "term": p.get("never")}}
        return {"result": "pass", "measured": {"hits": 0}}
    if kind == "structure":
        names = [str(s).lower() for s in (order or [])]
        try:
            i = names.index(str(p.get("section", "")).lower())
            j = names.index(str(p.get("anchor", "")).lower())
        except ValueError:
            return {"result": "flagged", "measured": {"reason": "section missing"}}
        ok = (i < j) if p.get("position", "before") == "before" else (i > j)
        return {
            "result": "pass" if ok else "flagged",
            "measured": {"section_index": i, "anchor_index": j},
        }
    if kind == "always_do":
        anchor = str(p.get("step", ""))
        if anchor and anchor.lower() in text.lower():
            return {"result": "pass", "measured": {}}
        return {"result": "flagged", "measured": {"reason": "step not found in output"}}
    if kind == "always_flag":
        facts = facts or {}
        raw = facts.get(str(p.get("metric", "")).lower())
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return {"result": "error", "measured": {"reason": "metric not numeric"}}
        threshold = float(p.get("threshold", 0))
        op = p.get("operator", "above")
        hit = (
            (value > threshold)
            if op == "above"
            else (value < threshold)
            if op == "below"
            else (value == threshold)
        )
        return {
            "result": "flagged" if hit else "pass",
            "measured": {"value": value, "threshold": threshold, "operator": op},
        }
    if kind == "outside_appetite":
        wanted = [str(t).lower() for t in (p.get("property_types") or [])]
        if property_type and property_type.lower() in wanted:
            return {"result": "flagged", "measured": {"property_type": property_type}}
        return {"result": "pass", "measured": {}}
    return {"result": "error", "measured": {"reason": "unknown kind"}}


def list_evaluations(tenant_id: str, rule_id: str, limit: int = 100) -> list[dict[str, Any]]:
    return guardrail_store.list_evaluations(tenant_id, rule_id, limit=limit)


def accuracy(tenant_id: str) -> dict[str, Any]:
    return guardrail_store.accuracy(tenant_id)
